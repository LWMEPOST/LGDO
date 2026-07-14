import { afterAll, beforeAll, describe, expect, test } from 'bun:test';
import { createHash } from 'node:crypto';
import { mkdirSync, realpathSync, rmSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { importFromContent } from '../../src/core/import-file.ts';
import {
  runLgdoVaultSync,
  type LgdoExpectedPage,
  type LgdoVaultSyncInput,
} from '../../src/core/lgdo-vault-sync.ts';
import type { OperationContext } from '../../src/core/operations.ts';
import { PGLiteEngine } from '../../src/core/pglite-engine.ts';

let engine: PGLiteEngine;
const TASK4_SOURCE = 'lgdo-e2e';
const TASK4_CLIENT = 'lgdo-e2e-client';
const TASK4_TMP = join(import.meta.dir, '.tmp-lgdo-vault-sync-pglite');
const TASK4_ROOT = join(TASK4_TMP, 'vault', 'wiki');
let originalAllowedRoots: string | undefined;

function task4Hash(content: string): string {
  return createHash('sha256').update(content).digest('hex');
}

function task4Content(pageId: string, revisionId: string, body: string): string {
  return `---\nid: ${pageId}\nlgdo_page_id: ${pageId}\nlgdo_revision_id: ${revisionId}\ntype: concept\ntitle: E2E Page\n---\n\n${body}\n`;
}

function writeTask4Page(
  path: string,
  pageId: string,
  revisionId: string,
  body: string,
): LgdoExpectedPage {
  const content = task4Content(pageId, revisionId, body);
  const absolute = join(TASK4_ROOT, ...path.split('/'));
  mkdirSync(dirname(absolute), { recursive: true });
  writeFileSync(absolute, content, 'utf8');
  return {
    page_id: pageId,
    revision_id: revisionId,
    projection_epoch: 7,
    path,
    file_hash: task4Hash(content),
  };
}

function task4Context(): OperationContext {
  return {
    engine,
    config: {} as OperationContext['config'],
    logger: console as unknown as OperationContext['logger'],
    dryRun: false,
    remote: true,
    sourceId: TASK4_SOURCE,
    auth: {
      token: 'redacted',
      clientId: TASK4_CLIENT,
      scopes: ['write'],
      sourceId: TASK4_SOURCE,
    },
  };
}

function task4Input(
  mode: 'incremental' | 'reconcile',
  expectedPages: LgdoExpectedPage[],
  key: string,
): LgdoVaultSyncInput {
  return {
    source_id: TASK4_SOURCE,
    root: realpathSync(TASK4_ROOT),
    mode,
    expected_pages: expectedPages,
    protected_mappings: [],
    no_embed: true,
    idempotency_key: key,
  };
}

async function resetTask4State(): Promise<void> {
  rmSync(TASK4_TMP, { recursive: true, force: true });
  mkdirSync(TASK4_ROOT, { recursive: true });
  process.env.GBRAIN_IMPORT_ALLOWED_ROOTS = realpathSync(TASK4_ROOT);
  await engine.executeRaw('DELETE FROM lgdo_vault_sync_runs WHERE source_id = $1', [TASK4_SOURCE]);
  await engine.executeRaw('DELETE FROM pages WHERE source_id = $1', [TASK4_SOURCE]);
  await engine.executeRaw(
    `INSERT INTO sources (id, name, local_path, config, archived)
     VALUES ($1, $2, $3, $4::jsonb, false)
     ON CONFLICT (id) DO UPDATE SET
       local_path = EXCLUDED.local_path,
       config = EXCLUDED.config,
       archived = false`,
    [
      TASK4_SOURCE,
      'LGDO e2e source',
      realpathSync(TASK4_ROOT),
      JSON.stringify({ lgdo_managed: true, lgdo_projection_client_id: TASK4_CLIENT }),
    ],
  );
}

async function pageGeneration(slug: string): Promise<number> {
  const rows = await engine.executeRaw<{ generation: number }>(
    `SELECT generation FROM pages WHERE slug = $1 AND source_id = $2`,
    [slug, 'default'],
  );
  if (rows.length !== 1) throw new Error(`missing test page: ${slug}`);
  return Number(rows[0].generation);
}

beforeAll(async () => {
  originalAllowedRoots = process.env.GBRAIN_IMPORT_ALLOWED_ROOTS;
  engine = new PGLiteEngine();
  await engine.connect({ type: 'pglite' } as never);
  await engine.initSchema();
}, 60_000);

afterAll(async () => {
  if (originalAllowedRoots === undefined) delete process.env.GBRAIN_IMPORT_ALLOWED_ROOTS;
  else process.env.GBRAIN_IMPORT_ALLOWED_ROOTS = originalAllowedRoots;
  rmSync(TASK4_TMP, { recursive: true, force: true });
  if (engine) await engine.disconnect();
}, 60_000);

describe('LGDO import contracts against PGLite', () => {
  test('fresh schema includes the LGDO sync idempotency table', async () => {
    const rows = await engine.executeRaw<{ table_name: string }>(
      `SELECT table_name FROM information_schema.tables WHERE table_name = $1`,
      ['lgdo_vault_sync_runs'],
    );
    expect(rows).toHaveLength(1);
  });

  test('restores and refreshes a tombstone with a verifiable generation', async () => {
    const slug = 'wiki/lgdo-contract/restore-success';
    await engine.putPage(slug, {
      type: 'concept',
      title: 'Before restore',
      compiled_truth: 'old content',
    }, { sourceId: 'default' });
    const generationBefore = await pageGeneration(slug);
    await engine.softDeletePage(slug, { sourceId: 'default' });

    const result = await importFromContent(
      engine,
      slug,
      `---\ntype: concept\ntitle: After restore\n---\n\nFresh restored content.\n`,
      {
        noEmbed: true,
        sourceId: 'default',
        restoreDeleted: true,
        forceRechunk: true,
        forceGenerationBump: true,
      },
    );

    const restored = await engine.getPage(slug, {
      sourceId: 'default',
      includeDeleted: true,
    });
    expect(result.status).toBe('imported');
    expect(result.content_hash).toMatch(/^[a-f0-9]{64}$/);
    expect(result.page_generation).toBeGreaterThan(generationBefore);
    expect(restored?.deleted_at).toBeNull();
    expect(restored?.compiled_truth).toContain('Fresh restored content.');
    expect(await pageGeneration(slug)).toBe(result.page_generation!);
  });

  test('rolls back tombstone restore when chunk persistence fails', async () => {
    const slug = 'wiki/lgdo-contract/restore-rollback';
    await engine.putPage(slug, {
      type: 'concept',
      title: 'Rollback seed',
      compiled_truth: 'content that must survive rollback',
    }, { sourceId: 'default' });
    await engine.softDeletePage(slug, { sourceId: 'default' });
    const before = await engine.getPage(slug, {
      sourceId: 'default',
      includeDeleted: true,
    });
    const generationBefore = await pageGeneration(slug);
    expect(before?.deleted_at).not.toBeNull();

    const originalRestorePage = engine.restorePage;
    let restoreAttempts = 0;
    (engine as any).restorePage = async function (...args: Parameters<PGLiteEngine['restorePage']>) {
      restoreAttempts += 1;
      return originalRestorePage.call(this, ...args);
    };
    (engine as any).upsertChunks = async () => {
      throw new Error('forced chunk persistence failure');
    };

    try {
      await expect(
        importFromContent(
          engine,
          slug,
          `---\ntype: concept\ntitle: Rollback update\n---\n\ncontent that must roll back\n`,
          {
            noEmbed: true,
            sourceId: 'default',
            restoreDeleted: true,
            forceRechunk: true,
            forceGenerationBump: true,
          },
        ),
      ).rejects.toThrow('forced chunk persistence failure');
    } finally {
      delete (engine as any).restorePage;
      delete (engine as any).upsertChunks;
    }

    const after = await engine.getPage(slug, {
      sourceId: 'default',
      includeDeleted: true,
    });
    expect(restoreAttempts).toBe(1);
    expect(after?.deleted_at).not.toBeNull();
    expect(after?.compiled_truth).toBe(before?.compiled_truth);
    expect(await pageGeneration(slug)).toBe(generationBefore);
  });
});

describe('trusted LGDO Vault sync against PGLite', () => {
  test('restores a deleted page and force reimports the trusted revision', async () => {
    await resetTask4State();
    const expected = writeTask4Page('restore.md', 'page-restore', 'rev-new', 'restored through manifest sync');
    await engine.putPage('restore', {
      type: 'concept',
      title: 'Old restore page',
      compiled_truth: 'old tombstone content',
      source_path: 'restore.md',
      frontmatter: {
        id: 'page-restore',
        lgdo_page_id: 'page-restore',
        lgdo_revision_id: 'rev-old',
      },
    }, { sourceId: TASK4_SOURCE });
    await engine.softDeletePage('restore', { sourceId: TASK4_SOURCE });

    const result = await runLgdoVaultSync(
      task4Context(),
      task4Input('incremental', [expected], 'restore-through-sync'),
    );
    const page = await engine.getPage('restore', { sourceId: TASK4_SOURCE, includeDeleted: true });

    expect(result.pages[0].status).toBe('imported');
    expect(result.pages[0].page_generation).toBeGreaterThan(0);
    expect(page?.deleted_at).toBeNull();
    expect(page?.compiled_truth).toContain('restored through manifest sync');
  });

  test('marks a page superseded when raw bytes change after import', async () => {
    await resetTask4State();
    const expected = writeTask4Page('cas.md', 'page-cas', 'rev-1', 'content before post hash');
    const absolute = join(TASK4_ROOT, 'cas.md');
    const originalBump = engine.bumpPageGeneration;
    (engine as any).bumpPageGeneration = async function (
      ...args: Parameters<PGLiteEngine['bumpPageGeneration']>
    ) {
      const generation = await originalBump.call(this, ...args);
      writeFileSync(absolute, task4Content('page-cas', 'rev-2', 'changed during import'), 'utf8');
      return generation;
    };

    let result;
    try {
      result = await runLgdoVaultSync(
        task4Context(),
        task4Input('incremental', [expected], 'post-hash-cas'),
      );
    } finally {
      delete (engine as any).bumpPageGeneration;
    }

    expect(result.pages[0].status).toBe('superseded');
    expect(result.pages[0].raw_file_hash_before).toBe(expected.file_hash);
    expect(result.pages[0].raw_file_hash_after).not.toBe(expected.file_hash);
  });

  test('compensates a rename when refresh fails', async () => {
    await resetTask4State();
    const expected = writeTask4Page('new/location.md', 'page-rename', 'rev-new', 'new location content');
    await engine.putPage('old/location', {
      type: 'concept',
      title: 'Old location',
      compiled_truth: 'old location content',
      source_path: 'old/location.md',
      frontmatter: {
        id: 'page-rename',
        lgdo_page_id: 'page-rename',
        lgdo_revision_id: 'rev-old',
      },
    }, { sourceId: TASK4_SOURCE });
    (engine as any).transaction = async () => {
      throw new Error('forced refresh failure');
    };

    let result;
    try {
      result = await runLgdoVaultSync(
        task4Context(),
        task4Input('incremental', [expected], 'rename-compensation'),
      );
    } finally {
      delete (engine as any).transaction;
    }

    expect(result.pages[0].status).toBe('error');
    expect(result.pages[0].error).toContain('forced refresh failure');
    expect(await engine.getPage('old/location', { sourceId: TASK4_SOURCE, includeDeleted: true })).not.toBeNull();
    expect(await engine.getPage('new/location', { sourceId: TASK4_SOURCE, includeDeleted: true })).toBeNull();
  });

  test('returns recovery_required and persists both rename mappings through reconcile', async () => {
    await resetTask4State();
    const expected = writeTask4Page('new/recovery.md', 'page-recovery', 'rev-new', 'new recovery content');
    await engine.putPage('old/recovery', {
      type: 'concept',
      title: 'Old recovery',
      compiled_truth: 'old recovery content',
      source_path: 'old/recovery.md',
      frontmatter: {
        id: 'page-recovery',
        lgdo_page_id: 'page-recovery',
        lgdo_revision_id: 'rev-old',
      },
    }, { sourceId: TASK4_SOURCE });

    const originalUpdateSlug = engine.updateSlug;
    let renameCalls = 0;
    (engine as any).updateSlug = async function (...args: Parameters<PGLiteEngine['updateSlug']>) {
      renameCalls += 1;
      if (renameCalls === 2) return false;
      return originalUpdateSlug.call(this, ...args);
    };
    (engine as any).transaction = async () => {
      throw new Error('forced refresh failure before failed compensation');
    };

    let failed;
    try {
      failed = await runLgdoVaultSync(
        task4Context(),
        task4Input('incremental', [expected], 'recovery-required'),
      );
    } finally {
      delete (engine as any).updateSlug;
      delete (engine as any).transaction;
    }

    expect(failed.pages[0].status).toBe('recovery_required');
    expect(failed.pages[0].protected_mappings).toEqual(expect.arrayContaining([
      expect.objectContaining({ slug: 'old/recovery' }),
      expect.objectContaining({ slug: 'new/recovery' }),
    ]));

    await engine.putPage('old/recovery', {
      type: 'concept',
      title: 'Protected old placeholder',
      compiled_truth: 'must survive reconcile',
      source_path: 'old/recovery.md',
      frontmatter: { id: 'page-old-placeholder' },
    }, { sourceId: TASK4_SOURCE });
    rmSync(TASK4_ROOT, { recursive: true, force: true });
    mkdirSync(TASK4_ROOT, { recursive: true });

    const reconciled = await runLgdoVaultSync(
      task4Context(),
      task4Input('reconcile', [], 'recovery-reconcile'),
    );
    expect(reconciled.deleted).toEqual([]);
    expect(await engine.getPage('old/recovery', { sourceId: TASK4_SOURCE, includeDeleted: true })).not.toBeNull();
    expect(await engine.getPage('new/recovery', { sourceId: TASK4_SOURCE, includeDeleted: true })).not.toBeNull();
  });
});
