// PRL-MM process management (pm2). Mirrors the health project's pm2 setup so everything
// lives under one `pm2 list`. Run from this dir:  pm2 start ecosystem.config.js
//
// CUM-LOSS guard (ported from the systemd StartLimitBurst design):
//   auto_run writes .STOP on a real cumulative-loss halt and self-halts on reading it.
//   - normal ~16h time-budget halt: exits 0 after running >> min_uptime -> pm2 restarts once
//     cleanly (restart counter resets) and trading continues.
//   - CUM-LOSS halt: each restart re-reads .STOP and exits within seconds (< min_uptime);
//     after max_restarts fast exits pm2 marks it 'errored' and STOPS restarting -> parked
//     for a human/LLM to verify leg-based PnL (report.py) before `rm .STOP` + restart.
// Manual emergency stop:  `pm2 stop prl-auto-run`  OR  `touch .STOP` (self-halts <= poll interval).
const CWD = __dirname;
const PY = `${__dirname}/.venv/bin/python`;

module.exports = {
  apps: [
    {
      // Read-only market snapshot -> state.json + timeseries. Always safe to restart.
      name: 'prl-data-hub',
      script: PY,
      args: ['-u', 'data_hub.py'],
      cwd: CWD,
      watch: false,
      max_restarts: 10,
      restart_delay: 10000,
    },
    {
      // Inventory rebalancer / bridge driver. Has its own token-conservation guard.
      name: 'prl-supply',
      script: PY,
      args: ['-u', 'supply.py'],
      cwd: CWD,
      watch: false,
      max_restarts: 10,
      restart_delay: 10000,
    },
    {
      // The taker arbitrage trader. min_uptime + max_restarts replicate the systemd
      // CUM-LOSS park: fast repeated exits (.STOP present) -> pm2 'errored' (stops).
      name: 'prl-auto-run',
      script: PY,
      args: ['-u', 'scripts/auto_run.py'],
      cwd: CWD,
      watch: false,
      min_uptime: '120s',   // an exit sooner than this counts as a failed (unstable) restart
      max_restarts: 6,      // after 6 fast exits in a row -> errored/parked (CUM-LOSS .STOP loop)
      restart_delay: 10000,
    },
  ],
};
