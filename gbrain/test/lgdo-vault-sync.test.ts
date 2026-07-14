import { afterAll, beforeAll, beforeEach, describe, expect, test } from 'bun:test';
import { createHash } from 'node:crypto';
import { mkdirSync, realpathSync, rmSync, writeFileSync } from 'node:fs';
import { delimiter, dirname, join } from 'node:path';
import type { OperationContext } from '../src/core/operations.ts';
import { PGLiteEngine } from '../src/core/pglite-engine.ts';
import {
  resolveRegularManifestFile,
  runLgdoVaultSync,
  type LgdoExpectedPage,
  type LgdoVaultSyncInput,
} from '../src/core/lgdo-vault-sync.ts';

const TMP = join(import.meta.dir, '.tmp-lgdo-vault-sync');
const ROOT = join(TMP, 'vault', 'wiki');
const OTHER_ROOT = join(TMP, 'other-root');
const SOURCE_ID = 'lgdo-unit';
const CLIENT_ID = 'lgdo-projection-client';

let engine: PGLiteEngine;
let originalAllowedRoots: string | undefined;

function sha256(value: string | Buffer): string {
  return createHash('sha256').update(value).digest('hex');
}

function manifestContent(
  pageId: string,
  revisionId: string,
  overrides: {
    id?: string;
    lgdoPageId?: string;
    lgdoRevisionId?: string;
    slug?: string;
    body?: string;
  } = {},
): string {
  const slug = overrides.slug ? `slug: ${overrides.slug}\n` : '';
  return `---\nid: ${overrides.id ?? pageId}\nlgdo_page_id: ${overrides.lgdoPageId ?? pageId}\nlgdo_revision_id: ${overrides.lgdoRevisionId ?? revisionId}\ntype: concept\ntitle: LGDO Test\n${slug}---\n\n${overrides.body ?? 'trusted manifest content'}\n`;
}

function writeManifestPage(
  path: string,
  pageId = `page-${path.replace(/[^a-z0-9]/gi, '-')}`,
  revisionId = 'rev-1',
  overrides: Parameters<typeof manifestContent>[2] = {},
): LgdoExpectedPage {
  const content = manifestContent(pageId, revisionId, overrides);
  const absolute = join(ROOT, ...path.split('/'));
  mkdirSync(dirname(absolute), { recursive: true });
  writeFileSync(absolute, content, 'utf8');
  return {
    page_id: pageId,
    revision_id: revisionId,
    projection_epoch: 1,
    path,
    file_hash: sha256(content),
  };
}

function syncInput(
  mode: 'incremental' | 'reconcile',
  expectedPages: LgdoExpectedPage[] = [],
  overrides: Partial<LgdoVaultSyncInput> = {},
): LgdoVaultSyncInput {
  return {
    source_id: SOURCE_ID,
    root: realpathSync(ROOT),
    mode,
    expected_pages: expectedPages,
    protected_mappings: [],
    no_embed: true,
    idempotency_key: `${mode}-${Math.random().toString(16).slice(2)}`,
    ...overrides,
  };
}

function operationContext(
  overrides: { sourceId?: string; authSourceId?: string; clientId?: string } = {},
): OperationContext {
  const sourceId = overrides.sourceId ?? SOURCE_ID;
  return {
    engine,
    config: {} as OperationContext['config'],
    logger: console as unknown as OperationContext['logger'],
    dryRun: false,
    remote: true,
    sourceId,
    auth: {
      token: 'redacted',
      clientId: overrides.clientId ?? CLIENT_ID,
      scopes: ['write'],
      sourceId: overrides.authSourceId ?? SOURCE_ID,
    },
  };
}

async function configureSource(
  overrides: { managed?: boolean; clientId?: string; root?: string; archived?: boolean } = {},
): Promise<void> {
  const config = JSON.stringify({
    lgdo_managed: overrides.managed ?? true,
    lgdo_projection_client_id: overrides.clientId ?? CLIENT_ID,
  });
  await engine.executeRaw(
    `INSERT INTO sources (id, name, local_path, config, archived)
     VALUES ($1, $2, $3, $4::jsonb, $5)
     ON CONFLICT (id) DO UPDATE SET
       name = EXCLUDED.name,
       local_path = EXCLUDED.local_path,
       config = EXCLUDED.config,
       archived = EXCLUDED.archived`,
    [
      SOURCE_ID,
      'LGDO unit source',
      overrides.root ?? realpathSync(ROOT),
      config,
      overrides.archived ?? false,
    ],
  );
}

async function expectSyncError(
  promise: Promise<unknown>,
  message: RegExp,
  code = 'invalid_params',
): Promise<void> {
  let caught: unknown;
  try {
    await promise;
  } catch (error) {
    caught = error;
  }
  expect(caught).toBeInstanceOf(Error);
  expect((caught as Error).message).toMatch(message);
  expect((caught as { code?: string }).code).toBe(code);
}

beforeAll(async () => {
  originalAllowedRoots = process.env.GBRAIN_IMPORT_ALLOWED_ROOTS;
  engine = new PGLiteEngine();
  await engine.connect({ type: 'pglite' } as never);
  await engine.initSchema();
}, 60_000);

beforeEach(async () => {
  rmSync(TMP, { recursive: true, force: true });
  mkdirSync(ROOT, { recursive: true });
  mkdirSync(OTHER_ROOT, { recursive: true });
  process.env.GBRAIN_IMPORT_ALLOWED_ROOTS = realpathSync(ROOT);
  await engine.executeRaw('DELETE FROM lgdo_vault_sync_runs WHERE source_id = $1', [SOURCE_ID]);
  await engine.executeRaw('DELETE FROM pages WHERE source_id = $1', [SOURCE_ID]);
  await configureSource();
});

afterAll(async () => {
  if (originalAllowedRoots === undefined) delete process.env.GBRAIN_IMPORT_ALLOWED_ROOTS;
  else process.env.GBRAIN_IMPORT_ALLOWED_ROOTS = originalAllowedRoots;
  rmSync(TMP, { recursive: true, force: true });
  if (engine) await engine.disconnect();
}, 60_000);

describe('LGDO trusted-manifest request validation', () => {
  test('rejects a canonical root mismatch', async () => {
    process.env.GBRAIN_IMPORT_ALLOWED_ROOTS = [realpathSync(ROOT), realpathSync(OTHER_ROOT)].join(delimiter);
    await expectSyncError(
      runLgdoVaultSync(operationContext(), syncInput('reconcile', [], { root: realpathSync(OTHER_ROOT) })),
      /source local_path/i,
    );
  });

  test('rejects a canonical root that is not a directory', async () => {
    const rootFile = join(TMP, 'vault-file');
    writeFileSync(rootFile, 'not a directory', 'utf8');
    const canonicalFile = realpathSync(rootFile);
    process.env.GBRAIN_IMPORT_ALLOWED_ROOTS = canonicalFile;
    await configureSource({ root: canonicalFile });

    await expectSyncError(
      runLgdoVaultSync(
        operationContext(),
        syncInput('reconcile', [], { root: canonicalFile, idempotency_key: 'root-file' }),
      ),
      /accessible directory/i,
    );
  });

  test('rejects a root outside GBRAIN_IMPORT_ALLOWED_ROOTS', async () => {
    process.env.GBRAIN_IMPORT_ALLOWED_ROOTS = realpathSync(OTHER_ROOT);
    await expectSyncError(
      runLgdoVaultSync(operationContext(), syncInput('reconcile')),
      /allowed roots/i,
    );
  });

  test('rejects a non-managed or archived source', async () => {
    await configureSource({ managed: false });
    await expectSyncError(runLgdoVaultSync(operationContext(), syncInput('reconcile')), /not LGDO managed/i);

    await configureSource({ archived: true });
    await expectSyncError(runLgdoVaultSync(operationContext(), syncInput('reconcile')), /archived/i);
  });

  test('rejects source and projection-client mismatches', async () => {
    await expectSyncError(
      runLgdoVaultSync(operationContext({ clientId: 'wrong-client' }), syncInput('reconcile')),
      /projection client/i,
    );
    await expectSyncError(
      runLgdoVaultSync(operationContext({ sourceId: 'other-source' }), syncInput('reconcile')),
      /context source/i,
    );
    await expectSyncError(
      runLgdoVaultSync(operationContext({ authSourceId: 'other-source' }), syncInput('reconcile')),
      /auth source/i,
    );
  });

  test('rejects more than 100 expected pages', async () => {
    const pages = Array.from({ length: 101 }, (_, index) => ({
      page_id: `page-${index}`,
      revision_id: 'rev-1',
      projection_epoch: 1,
      path: `bulk/${index}.md`,
      file_hash: '0'.repeat(64),
    }));
    await expectSyncError(runLgdoVaultSync(operationContext(), syncInput('incremental', pages)), /at most 100/i);
  });

  test('rejects duplicate page IDs and duplicate paths', async () => {
    const first = {
      page_id: 'duplicate-page', revision_id: 'rev-1', projection_epoch: 1,
      path: 'one.md', file_hash: '0'.repeat(64),
    };
    await expectSyncError(
      runLgdoVaultSync(operationContext(), syncInput('incremental', [first, { ...first, path: 'two.md' }])),
      /duplicate page_id/i,
    );
    await expectSyncError(
      runLgdoVaultSync(operationContext(), syncInput('incremental', [first, { ...first, page_id: 'other-page' }])),
      /duplicate path/i,
    );
  });

  test('rejects absolute, traversal, wiki-prefixed, backslash, dot, and empty-segment paths', async () => {
    const badPaths = [
      '/absolute.md',
      '../traversal.md',
      'wiki/product/demo.md',
      'product\\demo.md',
      'product/./demo.md',
      'product//demo.md',
    ];
    for (const [index, path] of badPaths.entries()) {
      const page = {
        page_id: `bad-path-${index}`,
        revision_id: 'rev-1',
        projection_epoch: 1,
        path,
        file_hash: '0'.repeat(64),
      };
      await expectSyncError(runLgdoVaultSync(operationContext(), syncInput('incremental', [page])), /path/i);
    }
  });

  test('rejects an empty incremental manifest but accepts empty reconcile control', async () => {
    await expectSyncError(runLgdoVaultSync(operationContext(), syncInput('incremental')), /cannot be empty/i);
    const result = await runLgdoVaultSync(operationContext(), syncInput('reconcile'));
    expect(result.pages).toEqual([]);
  });
});

describe('LGDO manifest file validation', () => {
  test('rejects a symlink file and a symlink ancestor', async () => {
    const directoryStat = { isSymbolicLink: () => false, isDirectory: () => true, isFile: () => false };
    const fileStat = { isSymbolicLink: () => false, isDirectory: () => false, isFile: () => true };
    const symlinkStat = { isSymbolicLink: () => true, isDirectory: () => false, isFile: () => false };

    await expect(
      resolveRegularManifestFile(ROOT, 'nested/demo.md', async (path) =>
        path.endsWith('demo.md') ? symlinkStat as any : directoryStat as any),
    ).rejects.toThrow(/symlink/i);
    await expect(
      resolveRegularManifestFile(ROOT, 'nested/demo.md', async (path) =>
        path.endsWith('nested') ? symlinkStat as any : fileStat as any),
    ).rejects.toThrow(/symlink/i);
  });

  test('returns a deterministic page error for a file larger than 5 MiB', async () => {
    const path = 'oversized.md';
    const content = Buffer.concat([
      Buffer.from(manifestContent('page-oversized', 'rev-1')),
      Buffer.alloc(5_000_001, 0x61),
    ]);
    writeFileSync(join(ROOT, path), content);
    const expected = {
      page_id: 'page-oversized', revision_id: 'rev-1', projection_epoch: 1,
      path, file_hash: sha256(content),
    };

    const result = await runLgdoVaultSync(operationContext(), syncInput('incremental', [expected]));
    expect(result.pages[0].status).toBe('error');
    expect(result.pages[0].error).toMatch(/5 MiB|too large/i);
  });

  test('rejects mismatched id, lgdo_page_id, lgdo_revision_id, and raw hash per page', async () => {
    const cases = [
      { name: 'id', overrides: { id: 'wrong-id' }, error: /frontmatter id/i },
      { name: 'lgdo-page', overrides: { lgdoPageId: 'wrong-page' }, error: /lgdo_page_id/i },
      { name: 'revision', overrides: { lgdoRevisionId: 'wrong-revision' }, error: /lgdo_revision_id/i },
      { name: 'hash', overrides: {}, error: /file hash/i, badHash: true },
    ];

    for (const item of cases) {
      const expected = writeManifestPage(`${item.name}.md`, `page-${item.name}`, 'rev-1', item.overrides);
      if (item.badHash) expected.file_hash = '0'.repeat(64);
      const result = await runLgdoVaultSync(
        operationContext(),
        syncInput('incremental', [expected], { idempotency_key: `mismatch-${item.name}` }),
      );
      expect(result.pages[0].status).toBe('error');
      expect(result.pages[0].error).toMatch(item.error);
    }
  });

  test('enforces the path-authoritative slug rule', async () => {
    const expected = writeManifestPage('product/demo.md', 'page-slug', 'rev-1', { slug: 'people/hijack' });
    const result = await runLgdoVaultSync(operationContext(), syncInput('incremental', [expected]));
    expect(result.pages[0].status).toBe('error');
    expect(result.pages[0].error).toMatch(/path-derived slug/i);
  });

  test('reports a missing manifest file as a deterministic page error', async () => {
    const expected = {
      page_id: 'page-missing', revision_id: 'rev-1', projection_epoch: 1,
      path: 'missing.md', file_hash: '0'.repeat(64),
    };
    const result = await runLgdoVaultSync(operationContext(), syncInput('incremental', [expected]));
    expect(result.pages[0].status).toBe('error');
    expect(result.errors).toBe(1);
    expect(result.pages[0].protected_mappings).toEqual(expect.arrayContaining([
      expect.objectContaining({ source_path: 'missing.md' }),
    ]));
  });
});

describe('LGDO manifest authority, reconcile, and idempotency', () => {
  test('never imports a valid unlisted Markdown file in incremental or reconcile mode', async () => {
    const listed = writeManifestPage('listed.md', 'page-listed');
    writeManifestPage('unlisted.md', 'page-unlisted');

    await runLgdoVaultSync(operationContext(), syncInput('incremental', [listed], { idempotency_key: 'listed-inc' }));
    await runLgdoVaultSync(operationContext(), syncInput('reconcile', [listed], { idempotency_key: 'listed-rec' }));

    const rows = await engine.executeRaw<{ slug: string }>(
      'SELECT slug FROM pages WHERE source_id = $1 ORDER BY slug',
      [SOURCE_ID],
    );
    expect(rows.map((row) => row.slug)).toEqual(['listed']);
  });

  test('ignores a parent .gitignore when importing an authorized file', async () => {
    writeFileSync(join(TMP, '.gitignore'), 'vault/\n', 'utf8');
    const expected = writeManifestPage('ignored/by/git.md', 'page-gitignore');
    const result = await runLgdoVaultSync(operationContext(), syncInput('incremental', [expected]));
    expect(result.pages[0].status).toBe('imported');
  });

  test('empty reconcile deletes absent source-scoped pages', async () => {
    await engine.putPage('ghost', {
      type: 'concept',
      title: 'Ghost',
      compiled_truth: 'remove me',
      source_path: 'ghost.md',
      frontmatter: { id: 'page-ghost', lgdo_page_id: 'page-ghost', lgdo_revision_id: 'rev-old' },
    }, { sourceId: SOURCE_ID });

    const result = await runLgdoVaultSync(operationContext(), syncInput('reconcile'));
    expect(result.deleted).toEqual([{ source_id: SOURCE_ID, slug: 'ghost' }]);
    expect(await engine.getPage('ghost', { sourceId: SOURCE_ID, includeDeleted: true })).toBeNull();
  });

  test('protects every existing mapping when an external ID is ambiguous', async () => {
    const pageId = 'page-ambiguous';
    const frontmatter = {
      id: pageId,
      lgdo_page_id: pageId,
      lgdo_revision_id: 'rev-old',
    };
    await engine.putPage('old-one', {
      type: 'concept',
      title: 'Old one',
      compiled_truth: 'first ambiguous page',
      source_path: 'old/one.md',
      frontmatter,
    }, { sourceId: SOURCE_ID });
    await engine.putPage('old-two', {
      type: 'concept',
      title: 'Old two',
      compiled_truth: 'second ambiguous page',
      source_path: 'old/two.md',
      frontmatter,
    }, { sourceId: SOURCE_ID });
    const expected = writeManifestPage('current.md', pageId, 'rev-new');

    const result = await runLgdoVaultSync(
      operationContext(),
      syncInput('reconcile', [expected], { idempotency_key: 'ambiguous-external-id' }),
    );

    expect(result.pages[0].status).toBe('error');
    expect(result.pages[0].error).toBe(`multiple pages match external id: ${pageId}`);
    expect(result.pages[0].protected_mappings).toEqual(expect.arrayContaining([
      expect.objectContaining({ slug: 'old-one', source_path: 'old/one.md' }),
      expect.objectContaining({ slug: 'old-two', source_path: 'old/two.md' }),
    ]));
    expect(result.deleted).toEqual([]);
    const remaining = await engine.executeRaw<{ slug: string; source_path: string | null }>(
      'SELECT slug, source_path FROM pages WHERE source_id = $1 ORDER BY slug',
      [SOURCE_ID],
    );
    expect(remaining).toEqual([
      { slug: 'old-one', source_path: 'old/one.md' },
      { slug: 'old-two', source_path: 'old/two.md' },
    ]);
  });

  test('replays a complete result for the same idempotency request and rejects conflicting reuse', async () => {
    const expected = writeManifestPage('idempotent.md', 'page-idempotent');
    const input = syncInput('incremental', [expected], { idempotency_key: 'stable-key' });
    const first = await runLgdoVaultSync(operationContext(), input);
    const second = await runLgdoVaultSync(operationContext(), input);
    expect(second).toEqual(first);

    await expectSyncError(
      runLgdoVaultSync(operationContext(), { ...input, no_embed: false }),
      /idempotency conflict/i,
      'idempotency_conflict',
    );
  });
});
