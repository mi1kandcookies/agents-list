// Names sidecar: issues ENSv2 names for agents and jobs on Ethereum Sepolia.
// Binds to 127.0.0.1 only; see README.md for setup and the live runbook.
import { createServer } from 'node:http';

import { loadConfig } from './lib/config.mjs';
import { DryRunExecutor, LiveExecutor } from './lib/executor.mjs';
import { createHandler } from './lib/http.mjs';
import { NamesService } from './lib/service.mjs';
import { NameStore } from './lib/store.mjs';

export async function buildService(config) {
  const exec = config.dryRun
    ? new DryRunExecutor()
    : new LiveExecutor({ privateKey: config.privateKey, rpcUrl: config.rpcUrl });
  const service = new NamesService({
    exec, store: new NameStore({ file: config.stateFile }), rootLabel: config.rootLabel,
  });
  await service.init();
  return service;
}

export async function start(config = loadConfig()) {
  const service = await buildService(config);
  const server = createServer(createHandler(service, { token: config.token }));
  await new Promise((resolve) => server.listen(config.port, config.host, resolve));
  const check = service.addressCheck;
  console.log(`[names] ${service.exec.mode} sidecar on http://${config.host}:${server.address().port}`
    + ` root=${service.rootName} operator=${service.exec.operator}`);
  if (!check.ok) console.error(`[names] address check FAILED, writes disabled: ${check.problems.join('; ')}`);
  return { server, service };
}

if (import.meta.url === `file://${process.argv[1]}`) {
  start().catch((err) => {
    console.error(`[names] failed to start: ${err.message}`);
    process.exit(1);
  });
}
