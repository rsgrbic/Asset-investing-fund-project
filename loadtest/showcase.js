import http from 'k6/http';
import { check, sleep } from 'k6';

const HOST = '20.215.32.142.sslip.io';
const AUTH = `https://auth.${HOST}`;
const EMP  = `https://employee.${HOST}`;
const DIR  = `https://director.${HOST}`;

//   kubectl port-forward --address 0.0.0.0 -n iep svc/ganache 8545:8545
const RPC = 'http://host.docker.internal:8545';

const DIRECTOR = { email: 'onlymoney@gmail.com', password: 'evenmoremoney' };

// These four numbers are one system, not four knobs. `ordering` makes ~90
// orders by 3:00, and the drain has 2 minutes for VOTES_PER_BATCH * BATCH_COUNT
// of them. Raise either and the scenario is cut off mid-batch with no error.
const VOTES_PER_BATCH = 10;   // peak of iep_voting_threads_active
const BATCH_COUNT     = 4;
// necessary for prometheus gauges
const THREAD_HOLD = 5;
// Split of the drain, so both outcome labels get series.
const APPROVE_RATE = 0.7;

const GAS = '0x2dc6c0'; // 3,000,000
const JSON_HDR = { headers: { 'Content-Type': 'application/json' } };

function bearer(token) {
  return { headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` } };
}

function rpc(method, params) {
  const res = http.post(RPC, JSON.stringify({ jsonrpc: '2.0', id: 1, method, params }), JSON_HDR);
  return res.json();
}

function castVote(from, tx) {
  const out = rpc('eth_sendTransaction', [{ from, to: tx.to, data: tx.data, gas: GAS }]);
  if (!out.error) return;
  // Expected once two voters agree: the contract refuses further votes.
  if (String(out.error.message).indexOf('Voting ended') !== -1) return;
  console.error(`vote failed from ${from}: ${JSON.stringify(out.error)}`);
}

export const options = {
  // No thresholds. A showcase must not fail the run.
  scenarios: {
    // Bulk of the traffic. The ramp is what gives the graph a readable shape.
    browsing: {
      executor: 'ramping-arrival-rate',
      startRate: 10, timeUnit: '1s',
      preAllocatedVUs: 50, maxVUs: 400,
      exec: 'search',
      stages: [
        { duration: '1m',    target: 60  },
        { duration: '1m30s', target: 60  }, // scales to 3, the observed ceiling
        { duration: '1m',    target: 150 },
        { duration: '1m',    target: 150 }, // past maxReplicas, latency degrades
        { duration: '30s',   target: 0   },
      ],
    },
    // ~90 orders. iep_pending_orders climbs while this runs.
    ordering: {
      executor: 'constant-arrival-rate',
      rate: 1, timeUnit: '2s', duration: '3m',
      preAllocatedVUs: 5, maxVUs: 20,
      exec: 'buyOrder',
    },
    // /report is the only thing that refreshes iep_assets_value.
    directorReads: {
      executor: 'constant-arrival-rate',
      rate: 1, timeUnit: '10s', duration: '5m',
      preAllocatedVUs: 2, maxVUs: 5,
      exec: 'directorRead',
    },
    // Starts once orders have piled up. One VU, so no two VUs claim the same
    // order. Concurrency comes from the batch, not from parallel VUs.
    draining: {
      executor: 'per-vu-iterations',
      vus: 1, iterations: BATCH_COUNT,
      startTime: '3m', maxDuration: '2m',
      exec: 'drainBatch',
    },
  },
};

export function setup() {
  const employee = {
    forename: 'load', surname: 'test',
    // Not a .local domain: email_validator rejects special-use names, register
    // returns 400, and every employee request afterwards is a 401.
    email: `synthetic-${Date.now()}@gmail.com`,
    password: 'aA123456',
  };

  const reg = http.post(`${AUTH}/register`, JSON.stringify(employee), JSON_HDR);
  if (reg.status !== 200) {
    throw new Error(`register failed: ${reg.status} ${reg.body}`);
  }

  const empLogin = http.post(`${AUTH}/login`, JSON.stringify(employee), JSON_HDR);
  const dirLogin = http.post(`${AUTH}/login`, JSON.stringify(DIRECTOR), JSON_HDR);
  const empToken = empLogin.json('accessToken');
  const dirToken = dirLogin.json('accessToken');
  // Throwing here aborts the run. Without it a bad token still produces five
  // minutes of 401s that look like traffic.
  if (!empToken || !dirToken) {
    throw new Error(`login failed: employee ${empLogin.status}, director ${dirLogin.status}`);
  }

  // Ganache leaves its accounts unlocked, so the node signs and no private key
  // is handled here. accounts[0] is the contract deployer.
  const accounts = rpc('eth_accounts', []).result;
  const voters = accounts ? accounts.slice(1, 4) : [];
  if (voters.length < 3) {
    console.error(`no ganache accounts at ${RPC} - the draining scenario will be skipped`);
  } else {
    console.log(`ganache reachable, voters ${voters.join(' ')}`);
  }

  return { empToken, dirToken, voters };
}

export function search(data) {
  const res = http.post(`${EMP}/search`, '{}', bearer(data.empToken));
  check(res, { 'search 200': (r) => r.status === 200 });
}

export function buyOrder(data) {
  const res = http.post(`${EMP}/create_buy_order`, JSON.stringify({
    name: `synthetic-${__VU}-${__ITER}`,
    categories: ['finance'],
    buying_price: 100,
    info: { issuer: { country: 'US' } },
  }), bearer(data.empToken));
  check(res, { 'order 200': (r) => r.status === 200 });
}

export function directorRead(data) {
  http.get(`${DIR}/pending_orders`, bearer(data.dirToken));
  http.get(`${DIR}/report`,         bearer(data.dirToken));
}

// draining runs on one VU, so this is claimed exactly once. Re-fetching per
// batch let a later batch reclaim an order whose watcher had not yet deleted
// the key, and /decision answered "Invalid uuid".
let queue = null;

export function drainBatch(data) {
  if (data.voters.length < 3) return;

  if (queue === null) {
    const pending = http.get(`${DIR}/pending_orders`, bearer(data.dirToken));
    queue = pending.json('orders').map((o) => o.uuid);
  }

  // Open every vote before sending any, so the watcher threads overlap.
  const ballots = [];
  for (const uuid of queue.splice(0, VOTES_PER_BATCH)) {
    const dec = http.post(`${DIR}/decision`, JSON.stringify({
      uuid, voters: data.voters,
    }), bearer(data.dirToken));

    if (!check(dec, { 'decision 200': (r) => r.status === 200 })) {
      console.error(`decision failed for ${uuid}: ${dec.status} ${dec.body}`);
      continue;
    }

    const approveTx = dec.json('approve_transaction');
    const rejectTx  = dec.json('reject_transaction');
    // Each voter decides on their own and the majority wins, which is what the
    // contract is built for. All three vote, so a 1-1 split cannot stall.
    ballots.push(data.voters.map((from) => ({
      from, tx: Math.random() < APPROVE_RATE ? approveTx : rejectTx,
    })));
  }

  sleep(THREAD_HOLD);

  // castApprove and castReject take no arguments, so the calldata is the same
  // for every voter. Only the sender changes.
  for (const votes of ballots) {
    for (const vote of votes) {
      castVote(vote.from, vote.tx);
    }
  }
  console.log(`batch: ${ballots.length} orders decided`);
}

export function teardown(data) {
  http.post(`${AUTH}/delete`, null, bearer(data.empToken));
  const pending = http.get(`${DIR}/pending_orders`, bearer(data.dirToken));
  console.log(`orders left pending: ${pending.json('orders').length}`);
}

export function handleSummary(data) {
  return { '/scripts/summary.json': JSON.stringify(data, null, 2) };
}
