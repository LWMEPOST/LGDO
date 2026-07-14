import { describe, expect, test } from 'bun:test';
import express from 'express';
import type { RequestHandler } from 'express';
import type { AddressInfo } from 'node:net';
import * as serveHttp from '../src/commands/serve-http.ts';
import {
  operations,
  operationsByName,
  type AuthInfo,
  type Operation,
  type OperationContext,
} from '../src/core/operations.ts';

type AuthorizationDecision = { ok: true } | { ok: false; message: string };
type AuthorizeMcpOperation = (auth: AuthInfo, op: Operation) => AuthorizationDecision;

function authorizeHelper(): AuthorizeMcpOperation | undefined {
  return (serveHttp as typeof serveHttp & {
    authorizeMcpOperation?: AuthorizeMcpOperation;
  }).authorizeMcpOperation;
}

function mcpBodyParser(): RequestHandler | undefined {
  return (serveHttp as typeof serveHttp & {
    mcpJsonBodyParser?: RequestHandler;
  }).mcpJsonBodyParser;
}

function auth(overrides: Partial<AuthInfo> = {}): AuthInfo {
  return {
    token: 'redacted',
    clientId: 'lgdo-projection-client',
    scopes: ['write'],
    sourceId: 'lgdo-source',
    ...overrides,
  };
}

const projectionOperation: Operation = {
  name: 'lgdo_vault_sync',
  description: 'test fixture for transport authorization',
  mutating: true,
  scope: 'write',
  params: {},
  handler: async () => undefined,
};

describe('lgdo_vault_sync operation contract', () => {
  test('registers a write-scoped mutating MCP operation without CLI aliases', () => {
    const op = operationsByName.lgdo_vault_sync;
    expect(op, 'op registered: lgdo_vault_sync').toBeDefined();
    if (!op) return;

    expect(op.mutating).toBe(true);
    expect(op.scope).toBe('write');
    expect(op.localOnly).not.toBe(true);
    expect(op.cliHints).toBeUndefined();

    const localCliNames = operations.flatMap((candidate) => [
      candidate.cliHints?.name,
      ...(candidate.cliHints?.aliases ?? []),
    ]).filter((value): value is string => typeof value === 'string');
    expect(localCliNames).not.toContain('lgdo_vault_sync');
  });

  test('declares the complete trusted-manifest parameter contract', () => {
    const op = operationsByName.lgdo_vault_sync;
    expect(op, 'op registered: lgdo_vault_sync').toBeDefined();
    if (!op) return;

    expect(op.params).toMatchObject({
      source_id: { type: 'string', required: true },
      root: { type: 'string', required: true },
      mode: { type: 'string', required: true, enum: ['incremental', 'reconcile'] },
      expected_pages: { type: 'array', required: true, items: { type: 'object' } },
      protected_mappings: { type: 'array', required: true, items: { type: 'object' } },
      no_embed: { type: 'boolean', required: true },
      idempotency_key: { type: 'string', required: true },
    });
  });
});

describe('LGDO projection transport authorization', () => {
  test('rejects a read-only query token before dispatch', () => {
    const authorize = authorizeHelper();
    expect(typeof authorize, 'authorizeMcpOperation export').toBe('function');
    if (!authorize) return;

    expect(authorize(auth({ scopes: ['read'] }), projectionOperation)).toEqual({
      ok: false,
      message: "requires 'write'",
    });
  });

  test('rejects a legacy admin identity that is not source-bound', () => {
    const authorize = authorizeHelper();
    expect(typeof authorize, 'authorizeMcpOperation export').toBe('function');
    if (!authorize) return;

    expect(authorize(auth({ clientId: 'legacy-admin', scopes: ['admin'], sourceId: undefined }), projectionOperation)).toEqual({
      ok: false,
      message: 'projection token must be source-bound',
    });
  });

  test('admits the dedicated source-bound projection identity', () => {
    const authorize = authorizeHelper();
    expect(typeof authorize, 'authorizeMcpOperation export').toBe('function');
    if (!authorize) return;

    expect(authorize(auth(), projectionOperation)).toEqual({ ok: true });
  });

  test('registered handler refuses a projection token to write another source', async () => {
    const op = operationsByName.lgdo_vault_sync;
    expect(op, 'op registered: lgdo_vault_sync').toBeDefined();
    if (!op) return;

    const context = {
      engine: {} as OperationContext['engine'],
      config: {} as OperationContext['config'],
      logger: console as unknown as OperationContext['logger'],
      dryRun: false,
      remote: true,
      sourceId: 'other-source',
      auth: auth({ sourceId: 'other-source' }),
    } satisfies OperationContext;

    await expect(op.handler(context, {
      source_id: 'lgdo-source',
      root: 'C:/not-reached',
      mode: 'reconcile',
      expected_pages: [],
      protected_mappings: [],
      no_embed: true,
      idempotency_key: 'source-bound-rejection',
    })).rejects.toThrow('operation context source does not match input source_id');
  });
});

describe('MCP JSON body limit', () => {
  test('returns a JSON 413 envelope for a body over one MiB', async () => {
    const parser = mcpBodyParser();
    expect(typeof parser, 'mcpJsonBodyParser export').toBe('function');
    if (!parser) return;

    const app = express();
    app.post('/mcp', parser, (_req, res) => res.json({ ok: true }));
    const server = app.listen(0, '127.0.0.1');
    await new Promise<void>((resolve, reject) => {
      server.once('listening', resolve);
      server.once('error', reject);
    });

    try {
      const { port } = server.address() as AddressInfo;
      const response = await fetch(`http://127.0.0.1:${port}/mcp`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ payload: 'x'.repeat(1_048_576) }),
      });

      expect(response.status).toBe(413);
      expect(response.headers.get('content-type')).toContain('application/json');
      expect(await response.json()).toMatchObject({ error: 'payload_too_large' });
    } finally {
      await new Promise<void>((resolve, reject) => {
        server.close((error) => error ? reject(error) : resolve());
      });
    }
  });
});
