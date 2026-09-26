// Sidecar configuration, read once from the environment.
import { DEFAULT_ROOT_LABEL } from './constants.mjs';

const truthy = (v) => ['1', 'true', 'yes', 'on'].includes(String(v ?? '').trim().toLowerCase());

export function loadConfig(env = process.env) {
  const dryRun = truthy(env.DRY_RUN);
  return {
    dryRun,
    token: env.ENS_SIDECAR_TOKEN || '',
    host: '127.0.0.1', // never exposed beyond the host running the app
    port: Number(env.ENS_SIDECAR_PORT || 8787),
    rootLabel: (env.ENS_ROOT_LABEL || DEFAULT_ROOT_LABEL).trim().toLowerCase(),
    rpcUrl: env.SEPOLIA_RPC_URL || '',
    privateKey: env.ENS_OPERATOR_PRIVATE_KEY || '',
    // Live mode keeps deployed resolver/registry addresses across restarts so
    // half-finished operations can resume. Dry runs stay in memory.
    stateFile: env.ENS_STATE_FILE ?? (dryRun ? '' : '.state/names.json'),
  };
}
