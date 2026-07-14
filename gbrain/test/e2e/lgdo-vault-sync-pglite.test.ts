import { afterAll, beforeAll, describe, expect, test } from 'bun:test';
import { importFromContent } from '../../src/core/import-file.ts';
import { PGLiteEngine } from '../../src/core/pglite-engine.ts';

let engine: PGLiteEngine;

async function pageGeneration(slug: string): Promise<number> {
  const rows = await engine.executeRaw<{ generation: number }>(
    `SELECT generation FROM pages WHERE slug = $1 AND source_id = $2`,
    [slug, 'default'],
  );
  if (rows.length !== 1) throw new Error(`missing test page: ${slug}`);
  return Number(rows[0].generation);
}

beforeAll(async () => {
  engine = new PGLiteEngine();
  await engine.connect({ type: 'pglite' } as never);
  await engine.initSchema();
}, 60_000);

afterAll(async () => {
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
