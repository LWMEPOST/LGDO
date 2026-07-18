import { createHash } from 'node:crypto';
import { constants, type Stats } from 'node:fs';
import { lstat, open, readdir, realpath } from 'node:fs/promises';
import {
  basename,
  delimiter,
  isAbsolute,
  join,
  posix,
  relative,
  resolve,
  sep,
  win32,
} from 'node:path';
import { DELETE_BATCH_SIZE } from './engine-constants.ts';
import type { BrainEngine } from './engine.ts';
import { importFromContent, type ImportResult } from './import-file.ts';
import { parseMarkdown } from './markdown.ts';
import type { OperationContext } from './operations.ts';
import { executeRawJsonb } from './sql-query.ts';
import { fetchSource, parseSourceConfig } from './sources-load.ts';
import { slugifyPath } from './sync.ts';

const MAX_EXPECTED_PAGES = 100;
const MAX_MARKDOWN_BYTES = 5_000_000;

export type LgdoVaultSyncMode = 'incremental' | 'reconcile';
export type LgdoPageStatus = 'imported' | 'skipped' | 'error' | 'superseded' | 'recovery_required';

export interface LgdoExpectedPage {
  page_id: string;
  revision_id: string;
  projection_epoch: number;
  path: string;
  file_hash: string;
}

export interface LgdoProtectedMapping {
  source_id: string;
  slug?: string;
  source_path?: string;
  reason: string;
}

export interface LgdoVaultSyncInput {
  source_id: string;
  root: string;
  mode: LgdoVaultSyncMode;
  expected_pages: LgdoExpectedPage[];
  protected_mappings: LgdoProtectedMapping[];
  no_embed: boolean;
  idempotency_key: string;
}

export interface LgdoPageResult extends LgdoExpectedPage {
  source_id: string;
  slug: string | null;
  source_path: string;
  raw_file_hash_before: string | null;
  raw_file_hash_after: string | null;
  content_hash: string | null;
  page_generation: number | null;
  status: LgdoPageStatus;
  error: string | null;
  protected_mappings: LgdoProtectedMapping[];
}

export interface LgdoVaultSyncResult {
  source_id: string;
  mode: LgdoVaultSyncMode;
  idempotency_key: string;
  pages: LgdoPageResult[];
  deleted: Array<{ source_id: string; slug: string }>;
  protected_mappings: LgdoProtectedMapping[];
  imported: number;
  skipped: number;
  errors: number;
  chunks: number;
  duration_ms: number;
}

export class LgdoVaultSyncError extends Error {
  constructor(
    public readonly code: 'invalid_params' | 'idempotency_conflict',
    message: string,
  ) {
    super(message);
    this.name = 'LgdoVaultSyncError';
  }
}

class SupersededFileError extends Error {
  constructor(public readonly afterHash: string) {
    super('raw file changed while the manifest page was being imported');
    this.name = 'SupersededFileError';
  }
}

interface ExternalPageRow {
  id: number;
  source_id: string;
  slug: string;
  source_path: string | null;
  content_hash: string | null;
  generation: number | string | null;
  deleted_at: Date | string | null;
  external_id: string | null;
  lgdo_page_id: string | null;
  lgdo_revision_id: string | null;
}

class AmbiguousExternalIdError extends Error {
  constructor(
    pageId: string,
    public readonly matches: ExternalPageRow[],
  ) {
    super(`multiple pages match external id: ${pageId}`);
    this.name = 'AmbiguousExternalIdError';
  }
}

interface ProcessedPage {
  result: LgdoPageResult;
  chunks: number;
}

interface WalkResult {
  presence: Set<string>;
  protectedPaths: Set<string>;
  protectedPrefixes: Set<string>;
}

type LstatResult = Pick<Stats, 'isSymbolicLink' | 'isDirectory' | 'isFile'>;
export type LgdoLstat = (path: string) => Promise<LstatResult>;

let syncTail: Promise<void> = Promise.resolve();

async function withSyncMutex<T>(fn: () => Promise<T>): Promise<T> {
  const previous = syncTail;
  let release!: () => void;
  syncTail = new Promise<void>((resolvePromise) => {
    release = resolvePromise;
  });
  await previous.catch(() => undefined);
  try {
    return await fn();
  } finally {
    release();
  }
}

function invalid(message: string): never {
  throw new LgdoVaultSyncError('invalid_params', message);
}

function nonEmptyString(value: unknown, field: string): string {
  if (typeof value !== 'string' || value.trim() === '') invalid(`${field} must be a non-empty string`);
  return value;
}

export function validateLgdoRelativePath(value: unknown): string {
  const path = nonEmptyString(value, 'expected_pages[].path');
  if (path.includes('\\')) invalid(`manifest path must use forward slashes: ${path}`);
  if (isAbsolute(path) || win32.isAbsolute(path) || path.startsWith('/')) {
    invalid(`manifest path must be root-relative: ${path}`);
  }
  if (/^wiki\//i.test(path)) invalid(`manifest path must not retain the leading wiki/: ${path}`);
  const segments = path.split('/');
  if (segments.some((segment) => segment === '' || segment === '.' || segment === '..')) {
    invalid(`manifest path contains an empty, dot, or traversal segment: ${path}`);
  }
  if (posix.normalize(path) !== path) invalid(`manifest path is not canonical: ${path}`);
  if (!/\.md$/i.test(path)) invalid(`manifest path must name a Markdown file: ${path}`);
  return path;
}

function validateProtectedMapping(value: unknown, sourceId: string, index: number): void {
  if (!value || typeof value !== 'object') invalid(`protected_mappings[${index}] must be an object`);
  const mapping = value as Record<string, unknown>;
  if (nonEmptyString(mapping.source_id, `protected_mappings[${index}].source_id`) !== sourceId) {
    invalid(`protected_mappings[${index}] belongs to another source`);
  }
  nonEmptyString(mapping.reason, `protected_mappings[${index}].reason`);
  const hasSlug = typeof mapping.slug === 'string' && mapping.slug.trim() !== '';
  const hasPath = typeof mapping.source_path === 'string' && mapping.source_path.trim() !== '';
  if (!hasSlug && !hasPath) invalid(`protected_mappings[${index}] requires slug or source_path`);
  if (hasPath) validateLgdoRelativePath(mapping.source_path);
  if (hasSlug && ((mapping.slug as string).includes('\\') || (mapping.slug as string).includes('..'))) {
    invalid(`protected_mappings[${index}].slug is not canonical`);
  }
}

function validateInput(input: LgdoVaultSyncInput): void {
  if (!input || typeof input !== 'object') invalid('lgdo_vault_sync input must be an object');
  const sourceId = nonEmptyString(input.source_id, 'source_id');
  nonEmptyString(input.root, 'root');
  nonEmptyString(input.idempotency_key, 'idempotency_key');
  if (input.mode !== 'incremental' && input.mode !== 'reconcile') invalid('mode must be incremental or reconcile');
  if (typeof input.no_embed !== 'boolean') invalid('no_embed must be boolean');
  if (!Array.isArray(input.expected_pages)) invalid('expected_pages must be an array');
  if (!Array.isArray(input.protected_mappings)) invalid('protected_mappings must be an array');
  if (input.expected_pages.length > MAX_EXPECTED_PAGES) {
    invalid(`expected_pages accepts at most ${MAX_EXPECTED_PAGES} pages`);
  }
  if (input.mode === 'incremental' && input.expected_pages.length === 0) {
    invalid('incremental expected_pages cannot be empty');
  }

  const pageIds = new Set<string>();
  const paths = new Set<string>();
  for (const [index, value] of input.expected_pages.entries()) {
    if (!value || typeof value !== 'object') invalid(`expected_pages[${index}] must be an object`);
    const pageId = nonEmptyString(value.page_id, `expected_pages[${index}].page_id`);
    nonEmptyString(value.revision_id, `expected_pages[${index}].revision_id`);
    if (!Number.isInteger(value.projection_epoch) || value.projection_epoch < 0) {
      invalid(`expected_pages[${index}].projection_epoch must be a non-negative integer`);
    }
    const path = validateLgdoRelativePath(value.path);
    if (!/^[a-f0-9]{64}$/.test(value.file_hash)) {
      invalid(`expected_pages[${index}].file_hash must be a lowercase SHA-256 hash`);
    }
    if (pageIds.has(pageId)) invalid(`duplicate page_id in expected_pages: ${pageId}`);
    if (paths.has(path)) invalid(`duplicate path in expected_pages: ${path}`);
    pageIds.add(pageId);
    paths.add(path);
  }
  input.protected_mappings.forEach((mapping, index) => validateProtectedMapping(mapping, sourceId, index));
}

function pathKey(path: string): string {
  return process.platform === 'win32' ? path.toLowerCase() : path;
}

async function canonicalDirectory(path: string, field: string): Promise<string> {
  try {
    const canonical = await realpath(path);
    if (!(await lstat(canonical)).isDirectory()) invalid(`${field} is not an accessible directory`);
    return canonical;
  } catch (error) {
    if (error instanceof LgdoVaultSyncError) throw error;
    invalid(`${field} is not an accessible directory`);
  }
}

export async function validateManagedSource(
  engine: BrainEngine,
  ctx: OperationContext,
  input: LgdoVaultSyncInput,
): Promise<string> {
  if (ctx.sourceId !== input.source_id) invalid('operation context source does not match input source_id');
  if (!ctx.auth?.sourceId || ctx.auth.sourceId !== input.source_id) {
    invalid('auth source does not match input source_id');
  }
  const source = await fetchSource(engine, input.source_id);
  if (!source) invalid(`managed source not found: ${input.source_id}`);
  if (source.archived === true) invalid(`managed source is archived: ${input.source_id}`);
  const config = parseSourceConfig(source.config);
  if (config.lgdo_managed !== true) invalid(`source is not LGDO managed: ${input.source_id}`);
  const projectionClientId = config.lgdo_projection_client_id;
  if (typeof projectionClientId !== 'string' || projectionClientId.trim() === '') {
    invalid('source has no LGDO projection client configured');
  }
  if (!ctx.auth.clientId || ctx.auth.clientId !== projectionClientId) {
    invalid('caller is not the configured LGDO projection client');
  }
  if (!source.local_path) invalid('managed source has no local_path');

  const canonicalInput = await canonicalDirectory(input.root, 'root');
  const canonicalSource = await canonicalDirectory(source.local_path, 'source local_path');
  if (pathKey(resolve(input.root)) !== pathKey(canonicalInput)) invalid('root must be a canonical path');
  if (pathKey(resolve(source.local_path)) !== pathKey(canonicalSource)) {
    invalid('source local_path must be canonical');
  }
  if (pathKey(canonicalInput) !== pathKey(canonicalSource)) {
    invalid('root does not match source local_path');
  }

  const allowedRaw = process.env.GBRAIN_IMPORT_ALLOWED_ROOTS;
  if (!allowedRaw) invalid('GBRAIN_IMPORT_ALLOWED_ROOTS has no allowed roots');
  const allowedEntries = allowedRaw.split(delimiter).map((entry) => entry.trim()).filter(Boolean);
  if (allowedEntries.length === 0) invalid('GBRAIN_IMPORT_ALLOWED_ROOTS has no allowed roots');
  const allowedRoots: string[] = [];
  for (const entry of allowedEntries) {
    allowedRoots.push(await canonicalDirectory(entry, 'GBRAIN_IMPORT_ALLOWED_ROOTS entry'));
  }
  if (!allowedRoots.some((allowed) => pathKey(allowed) === pathKey(canonicalInput))) {
    invalid('root is not an exact member of the allowed roots');
  }
  return canonicalInput;
}

export async function resolveRegularManifestFile(
  root: string,
  relativePath: string,
  lstatImpl: LgdoLstat = lstat,
): Promise<string> {
  const validated = validateLgdoRelativePath(relativePath);
  const absolute = resolve(root, ...validated.split('/'));
  const confined = relative(root, absolute);
  if (
    confined === ''
    || confined.startsWith('..')
    || confined.startsWith(`..${sep}`)
    || pathKey(resolve(root, confined)) !== pathKey(absolute)
  ) {
    invalid(`manifest path escapes root: ${validated}`);
  }

  let current = root;
  const segments = validated.split('/');
  for (let index = 0; index < segments.length; index += 1) {
    current = join(current, segments[index]);
    let stat: LstatResult;
    try {
      stat = await lstatImpl(current);
    } catch {
      throw new Error(`manifest file is missing: ${validated}`);
    }
    if (stat.isSymbolicLink()) throw new Error(`symlink path components are not allowed: ${validated}`);
    if (index < segments.length - 1 && !stat.isDirectory()) {
      throw new Error(`manifest ancestor is not a directory: ${validated}`);
    }
    if (index === segments.length - 1 && !stat.isFile()) {
      throw new Error(`manifest path is not a regular file: ${validated}`);
    }
  }
  return absolute;
}

async function hashOpenFile(
  absolutePath: string,
  maxBytes: number,
  collectBytes: boolean,
): Promise<{ hash: string; bytes?: Buffer }> {
  const noFollow = process.platform === 'win32' ? 0 : (constants.O_NOFOLLOW ?? 0);
  const handle = await open(absolutePath, constants.O_RDONLY | noFollow);
  const hash = createHash('sha256');
  const chunks: Buffer[] = [];
  let total = 0;
  try {
    const stat = await handle.stat();
    if (!stat.isFile()) throw new Error('manifest path is not a regular file');
    const buffer = Buffer.allocUnsafe(64 * 1024);
    while (true) {
      const { bytesRead } = await handle.read(buffer, 0, buffer.length, null);
      if (bytesRead === 0) break;
      total += bytesRead;
      if (total > maxBytes) throw new Error('manifest file exceeds the 5 MiB limit');
      const chunk = buffer.subarray(0, bytesRead);
      hash.update(chunk);
      if (collectBytes) chunks.push(Buffer.from(chunk));
    }
  } finally {
    await handle.close();
  }
  return {
    hash: hash.digest('hex'),
    ...(collectBytes ? { bytes: Buffer.concat(chunks, total) } : {}),
  };
}

export async function sha256RegularFile(absolutePath: string, maxBytes = MAX_MARKDOWN_BYTES): Promise<string> {
  return (await hashOpenFile(absolutePath, maxBytes, false)).hash;
}

async function readTrustedMarkdown(absolutePath: string): Promise<{ hash: string; markdown: string }> {
  const read = await hashOpenFile(absolutePath, MAX_MARKDOWN_BYTES, true);
  let markdown: string;
  try {
    markdown = new TextDecoder('utf-8', { fatal: true }).decode(read.bytes!);
  } catch {
    throw new Error('manifest file is not valid UTF-8');
  }
  return { hash: read.hash, markdown };
}

export function resolveLgdoSlug(relativePath: string, markdown: string): string {
  const path = validateLgdoRelativePath(relativePath);
  const parsed = parseMarkdown(markdown, path);
  const expectedSlug = slugifyPath(path);
  if (expectedSlug === '') {
    if (parsed.slug) return parsed.slug;
    throw new Error(`manifest path produces no usable slug: ${path}`);
  }
  if (parsed.slug !== expectedSlug) {
    throw new Error(
      `frontmatter slug "${parsed.slug}" does not match path-derived slug "${expectedSlug}"`,
    );
  }
  return expectedSlug;
}

function validateManifestIdentity(expected: LgdoExpectedPage, markdown: string): string {
  const parsed = parseMarkdown(markdown, expected.path);
  if (parsed.frontmatter.id !== expected.page_id) {
    throw new Error('frontmatter id does not match manifest page_id');
  }
  if (parsed.frontmatter.lgdo_page_id !== expected.page_id) {
    throw new Error('frontmatter lgdo_page_id does not match manifest page_id');
  }
  if (parsed.frontmatter.lgdo_revision_id !== expected.revision_id) {
    throw new Error('frontmatter lgdo_revision_id does not match manifest revision_id');
  }
  return resolveLgdoSlug(expected.path, markdown);
}

export async function findPageByExternalId(
  engine: BrainEngine,
  sourceId: string,
  pageId: string,
  includeDeleted = true,
): Promise<ExternalPageRow | null> {
  const rows = await engine.executeRaw<ExternalPageRow>(
    `SELECT id, source_id, slug, source_path, content_hash, generation, deleted_at,
            frontmatter->>'id' AS external_id,
            frontmatter->>'lgdo_page_id' AS lgdo_page_id,
            frontmatter->>'lgdo_revision_id' AS lgdo_revision_id
       FROM pages
       WHERE source_id = $1
         AND frontmatter->>'id' = $2
         ${includeDeleted ? '' : 'AND deleted_at IS NULL'}
       ORDER BY id`,
    [sourceId, pageId],
  );
  if (rows.length > 1) throw new AmbiguousExternalIdError(pageId, rows);
  return rows[0] ?? null;
}

export async function verifyExternalIdentity(
  engine: BrainEngine,
  sourceId: string,
  pageId: string,
  slug: string,
): Promise<boolean> {
  try {
    const row = await findPageByExternalId(engine, sourceId, pageId, true);
    return row?.source_id === sourceId && row.slug === slug;
  } catch {
    return false;
  }
}

async function verifyImportedIdentity(
  engine: BrainEngine,
  expected: LgdoExpectedPage,
  sourceId: string,
  slug: string,
  imported: ImportResult,
  afterHash: string,
): Promise<ExternalPageRow> {
  if (afterHash !== expected.file_hash) throw new SupersededFileError(afterHash);
  if (!imported.content_hash || imported.page_generation === undefined) {
    throw new Error('import result omitted content hash or page generation');
  }
  const row = await findPageByExternalId(engine, sourceId, expected.page_id, true);
  if (!row) throw new Error('imported page cannot be found by external id');
  if (row.source_id !== sourceId) throw new Error('imported page source_id mismatch');
  if (row.slug !== slug) throw new Error('imported page slug mismatch');
  if (row.source_path !== expected.path) throw new Error('imported page source_path mismatch');
  if (row.external_id !== expected.page_id || row.lgdo_page_id !== expected.page_id) {
    throw new Error('imported page external identity mismatch');
  }
  if (row.lgdo_revision_id !== expected.revision_id) throw new Error('imported page revision mismatch');
  if (row.content_hash !== imported.content_hash) throw new Error('imported page content hash mismatch');
  if (row.generation === null || Number(row.generation) !== imported.page_generation) {
    throw new Error('imported page generation mismatch');
  }
  if (row.deleted_at !== null) throw new Error('imported page remains deleted');
  return row;
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function mappingKey(mapping: LgdoProtectedMapping): string {
  return `${mapping.source_id}\u0000${mapping.slug ?? ''}\u0000${mapping.source_path ?? ''}`;
}

function dedupeMappings(mappings: LgdoProtectedMapping[]): LgdoProtectedMapping[] {
  const unique = new Map<string, LgdoProtectedMapping>();
  for (const mapping of mappings) {
    const key = mappingKey(mapping);
    if (!unique.has(key)) unique.set(key, mapping);
  }
  return [...unique.values()].sort((a, b) => mappingKey(a).localeCompare(mappingKey(b)));
}

function failureMappings(
  expected: LgdoExpectedPage,
  sourceId: string,
  old: ExternalPageRow | null,
  newSlug: string | null,
  reason: string,
): LgdoProtectedMapping[] {
  const mappings: LgdoProtectedMapping[] = [{
    source_id: sourceId,
    ...(newSlug ? { slug: newSlug } : {}),
    source_path: expected.path,
    reason,
  }];
  if (old) {
    mappings.push({
      source_id: sourceId,
      slug: old.slug,
      ...(old.source_path ? { source_path: old.source_path } : {}),
      reason,
    });
  }
  return dedupeMappings(mappings);
}

function failedPageResult(
  expected: LgdoExpectedPage,
  sourceId: string,
  old: ExternalPageRow | null,
  newSlug: string | null,
  beforeHash: string | null,
  afterHash: string | null,
  error: unknown,
): LgdoPageResult {
  const status: LgdoPageStatus = error instanceof SupersededFileError ? 'superseded' : 'error';
  const observedAfter = error instanceof SupersededFileError ? error.afterHash : afterHash;
  const ambiguousMappings = error instanceof AmbiguousExternalIdError
    ? error.matches.map((match) => ({
      source_id: sourceId,
      slug: match.slug,
      ...(match.source_path ? { source_path: match.source_path } : {}),
      reason: 'ambiguous_external_id',
    }))
    : [];
  return {
    ...expected,
    source_id: sourceId,
    slug: newSlug,
    source_path: expected.path,
    raw_file_hash_before: beforeHash,
    raw_file_hash_after: observedAfter,
    content_hash: null,
    page_generation: null,
    status,
    error: errorMessage(error),
    protected_mappings: dedupeMappings([
      ...failureMappings(expected, sourceId, old, newSlug, `page_${status}`),
      ...ambiguousMappings,
    ]),
  };
}

function recoveryRequiredResult(
  expected: LgdoExpectedPage,
  sourceId: string,
  old: ExternalPageRow,
  newSlug: string,
  beforeHash: string | null,
  afterHash: string | null,
  error: unknown,
): LgdoPageResult {
  return {
    ...expected,
    source_id: sourceId,
    slug: newSlug,
    source_path: expected.path,
    raw_file_hash_before: beforeHash,
    raw_file_hash_after: error instanceof SupersededFileError ? error.afterHash : afterHash,
    content_hash: null,
    page_generation: null,
    status: 'recovery_required',
    error: errorMessage(error),
    protected_mappings: failureMappings(
      expected,
      sourceId,
      old,
      newSlug,
      'rename_recovery_required',
    ),
  };
}

async function processExpectedPage(
  engine: BrainEngine,
  root: string,
  input: LgdoVaultSyncInput,
  expected: LgdoExpectedPage,
): Promise<ProcessedPage> {
  let old: ExternalPageRow | null = null;
  let newSlug: string | null = slugifyPath(expected.path) || null;
  let beforeHash: string | null = null;
  let afterHash: string | null = null;
  let renamedFrom: string | null = null;

  try {
    old = await findPageByExternalId(engine, input.source_id, expected.page_id, true);
    const absolutePath = await resolveRegularManifestFile(root, expected.path);
    const read = await readTrustedMarkdown(absolutePath);
    beforeHash = read.hash;
    if (beforeHash !== expected.file_hash) throw new Error('raw file hash does not match manifest file_hash');
    newSlug = validateManifestIdentity(expected, read.markdown);

    try {
      if (old && old.slug !== newSlug) {
        const moved = await engine.updateSlug(old.slug, newSlug, { sourceId: input.source_id });
        if (!moved) throw new Error(`rename affected zero rows: ${old.slug}`);
        renamedFrom = old.slug;
        if (!await verifyExternalIdentity(engine, input.source_id, expected.page_id, newSlug)) {
          throw new Error(`forward rename identity verification failed: ${old.slug}`);
        }
      }

      const imported = await importFromContent(engine, newSlug, read.markdown, {
        sourceId: input.source_id,
        sourcePath: expected.path,
        filename: basename(expected.path, '.md'),
        noEmbed: input.no_embed,
        forceRechunk: true,
        restoreDeleted: true,
        forceGenerationBump: true,
      });
      afterHash = await sha256RegularFile(absolutePath, MAX_MARKDOWN_BYTES);
      if (afterHash !== expected.file_hash) throw new SupersededFileError(afterHash);
      const row = await verifyImportedIdentity(
        engine,
        expected,
        input.source_id,
        newSlug,
        imported,
        afterHash,
      );
      return {
        chunks: imported.chunks,
        result: {
          ...expected,
          source_id: input.source_id,
          slug: newSlug,
          source_path: expected.path,
          raw_file_hash_before: beforeHash,
          raw_file_hash_after: afterHash,
          content_hash: row.content_hash,
          page_generation: Number(row.generation),
          status: imported.status === 'skipped' ? 'skipped' : 'imported',
          error: null,
          protected_mappings: [],
        },
      };
    } catch (error) {
      if (renamedFrom !== null && old) {
        let restored = false;
        try {
          const compensated = await engine.updateSlug(newSlug, renamedFrom, { sourceId: input.source_id });
          restored = compensated
            ? await verifyExternalIdentity(engine, input.source_id, expected.page_id, renamedFrom)
            : false;
        } catch {
          restored = false;
        }
        if (!restored) {
          return {
            chunks: 0,
            result: recoveryRequiredResult(
              expected,
              input.source_id,
              old,
              newSlug,
              beforeHash,
              afterHash,
              error,
            ),
          };
        }
      }
      return {
        chunks: 0,
        result: failedPageResult(
          expected,
          input.source_id,
          old,
          newSlug,
          beforeHash,
          afterHash,
          error,
        ),
      };
    }
  } catch (error) {
    return {
      chunks: 0,
      result: failedPageResult(
        expected,
        input.source_id,
        old,
        newSlug,
        beforeHash,
        afterHash,
        error,
      ),
    };
  }
}

function canonicalJson(value: unknown): string {
  if (value === null || typeof value !== 'object') return JSON.stringify(value) ?? 'null';
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(',')}]`;
  const object = value as Record<string, unknown>;
  return `{${Object.keys(object)
    .filter((key) => object[key] !== undefined)
    .sort()
    .map((key) => `${JSON.stringify(key)}:${canonicalJson(object[key])}`)
    .join(',')}}`;
}

function requestHash(input: LgdoVaultSyncInput): string {
  return createHash('sha256').update(canonicalJson(input)).digest('hex');
}

function parseStoredResult(value: unknown): LgdoVaultSyncResult {
  let parsed = value;
  if (typeof parsed === 'string') {
    try {
      parsed = JSON.parse(parsed);
    } catch {
      throw new Error('stored LGDO sync result is malformed');
    }
  }
  if (!parsed || typeof parsed !== 'object') throw new Error('stored LGDO sync result is malformed');
  return parsed as LgdoVaultSyncResult;
}

async function loadIdempotentResult(
  engine: BrainEngine,
  input: LgdoVaultSyncInput,
  hash: string,
): Promise<LgdoVaultSyncResult | null> {
  const rows = await engine.executeRaw<{ request_hash: string; result_json: unknown }>(
    `SELECT request_hash, result_json
       FROM lgdo_vault_sync_runs
      WHERE source_id = $1 AND idempotency_key = $2`,
    [input.source_id, input.idempotency_key],
  );
  if (rows.length === 0) return null;
  if (rows[0].request_hash !== hash) {
    throw new LgdoVaultSyncError('idempotency_conflict', 'idempotency conflict: key was used for another request');
  }
  return parseStoredResult(rows[0].result_json);
}

async function storeIdempotentResult(
  engine: BrainEngine,
  input: LgdoVaultSyncInput,
  hash: string,
  result: LgdoVaultSyncResult,
): Promise<LgdoVaultSyncResult> {
  const inserted = await executeRawJsonb<{ result_json: unknown }>(
    engine,
    `INSERT INTO lgdo_vault_sync_runs
       (source_id, idempotency_key, request_hash, result_json)
     VALUES ($1, $2, $3, $4::jsonb)
     ON CONFLICT (source_id, idempotency_key) DO NOTHING
     RETURNING result_json`,
    [input.source_id, input.idempotency_key, hash],
    [result],
  );
  if (inserted.length === 1) return result;
  const stored = await loadIdempotentResult(engine, input, hash);
  if (!stored) throw new Error('failed to persist LGDO sync result');
  return stored;
}

async function walkManifestRoot(root: string, expectedPaths: Set<string>): Promise<WalkResult> {
  const presence = new Set<string>();
  const protectedPaths = new Set<string>();
  const protectedPrefixes = new Set<string>();

  async function walk(absoluteDir: string, relativeDir: string): Promise<void> {
    const entries = await readdir(absoluteDir, { withFileTypes: true });
    entries.sort((a, b) => a.name.localeCompare(b.name));
    for (const entry of entries) {
      const relativePath = relativeDir ? `${relativeDir}/${entry.name}` : entry.name;
      const absolutePath = join(absoluteDir, entry.name);
      const stat = await lstat(absolutePath);
      if (stat.isSymbolicLink()) {
        if (/\.md$/i.test(relativePath)) protectedPaths.add(relativePath);
        else protectedPrefixes.add(relativePath);
        continue;
      }
      if (stat.isDirectory()) {
        await walk(absolutePath, relativePath);
        continue;
      }
      if (!stat.isFile() || !/\.md$/i.test(relativePath)) continue;
      try {
        const path = validateLgdoRelativePath(relativePath);
        presence.add(path);
        if (!expectedPaths.has(path)) protectedPaths.add(path);
      } catch {
        protectedPaths.add(relativePath);
      }
    }
  }

  await walk(root, '');
  return { presence, protectedPaths, protectedPrefixes };
}

function mappingIndex(mappings: LgdoProtectedMapping[]): { slugs: Set<string>; paths: Set<string> } {
  const slugs = new Set<string>();
  const paths = new Set<string>();
  for (const mapping of mappings) {
    if (mapping.slug) slugs.add(mapping.slug);
    if (mapping.source_path) paths.add(mapping.source_path);
  }
  return { slugs, paths };
}

async function reconcileDeletedPages(
  engine: BrainEngine,
  root: string,
  sourceId: string,
  expectedPages: LgdoExpectedPage[],
  mappings: LgdoProtectedMapping[],
): Promise<{ deleted: Array<{ source_id: string; slug: string }>; walkerMappings: LgdoProtectedMapping[] }> {
  const expectedPaths = new Set(expectedPages.map((page) => page.path));
  const walk = await walkManifestRoot(root, expectedPaths);
  const walkerMappings: LgdoProtectedMapping[] = [...walk.protectedPaths].map((sourcePath) => ({
    source_id: sourceId,
    source_path: sourcePath,
    reason: expectedPaths.has(sourcePath) ? 'present_invalid' : 'present_unlisted',
  }));
  const indexed = mappingIndex([...mappings, ...walkerMappings]);
  const protectedExpectedPaths = new Set(expectedPages.map((page) => page.path));
  const rows = await engine.executeRaw<{ slug: string; source_path: string | null }>(
    `SELECT slug, source_path FROM pages WHERE source_id = $1 ORDER BY slug`,
    [sourceId],
  );
  const deletable = rows
    .filter((row) => {
      if (!row.source_path) return false;
      if (protectedExpectedPaths.has(row.source_path)) return false;
      if (walk.presence.has(row.source_path)) return false;
      if (indexed.slugs.has(row.slug) || indexed.paths.has(row.source_path)) return false;
      for (const prefix of walk.protectedPrefixes) {
        if (row.source_path === prefix || row.source_path.startsWith(`${prefix}/`)) return false;
      }
      return true;
    })
    .map((row) => row.slug);

  const deleted: Array<{ source_id: string; slug: string }> = [];
  for (let offset = 0; offset < deletable.length; offset += DELETE_BATCH_SIZE) {
    const batch = deletable.slice(offset, offset + DELETE_BATCH_SIZE);
    const confirmed = new Set(await engine.deletePages(batch, { sourceId }));
    for (const slug of batch) {
      if (confirmed.has(slug)) deleted.push({ source_id: sourceId, slug });
    }
    await Bun.sleep(0);
  }
  return { deleted, walkerMappings };
}

async function executeLgdoVaultSync(
  ctx: OperationContext,
  input: LgdoVaultSyncInput,
): Promise<LgdoVaultSyncResult> {
  const started = Date.now();
  const root = await validateManagedSource(ctx.engine, ctx, input);
  const hash = requestHash(input);
  const replay = await loadIdempotentResult(ctx.engine, input, hash);
  if (replay) return replay;

  const pages: LgdoPageResult[] = [];
  let chunks = 0;
  for (const expected of input.expected_pages) {
    const processed = await processExpectedPage(ctx.engine, root, input, expected);
    pages.push(processed.result);
    chunks += processed.chunks;
    await Bun.sleep(0);
  }

  const pageMappings = pages.flatMap((page) => page.protected_mappings);
  let protectedMappings = dedupeMappings([
    ...input.protected_mappings,
    ...pageMappings,
  ]);
  let deleted: Array<{ source_id: string; slug: string }> = [];
  if (input.mode === 'reconcile') {
    const reconciled = await reconcileDeletedPages(
      ctx.engine,
      root,
      input.source_id,
      input.expected_pages,
      protectedMappings,
    );
    deleted = reconciled.deleted;
    protectedMappings = dedupeMappings([...protectedMappings, ...reconciled.walkerMappings]);
  }

  const result: LgdoVaultSyncResult = {
    source_id: input.source_id,
    mode: input.mode,
    idempotency_key: input.idempotency_key,
    pages,
    deleted,
    protected_mappings: protectedMappings,
    imported: pages.filter((page) => page.status === 'imported').length,
    skipped: pages.filter((page) => page.status === 'skipped').length,
    errors: pages.filter((page) => !['imported', 'skipped'].includes(page.status)).length,
    chunks,
    duration_ms: Date.now() - started,
  };
  return storeIdempotentResult(ctx.engine, input, hash, result);
}

export async function runLgdoVaultSync(
  ctx: OperationContext,
  input: LgdoVaultSyncInput,
): Promise<LgdoVaultSyncResult> {
  validateInput(input);
  return withSyncMutex(() => executeLgdoVaultSync(ctx, input));
}
