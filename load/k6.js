// k6 -e MODE=starts|state|save -e BASE_URL=http://127.0.0.1:8000
//    -e EXAM_ID=<seed_load output> -e LOAD_PREFIX=load01 -e LOAD_PASSWORD=<secret>
import http from 'k6/http';
import exec from 'k6/execution';
import { check, fail, sleep } from 'k6';
import { Rate, Trend } from 'k6/metrics';

const mode = __ENV.MODE || 'state';
const base = (__ENV.BASE_URL || 'http://127.0.0.1:8000').replace(/\/$/, '');
const examId = __ENV.EXAM_ID;
const prefix = __ENV.LOAD_PREFIX;
const password = __ENV.LOAD_PASSWORD;
if (!['starts', 'state', 'save'].includes(mode) || !examId || !prefix || !password) {
  throw new Error('Set MODE=starts|state|save, EXAM_ID, LOAD_PREFIX and LOAD_PASSWORD');
}

export const options = {
  scenarios: {
    [mode]: mode === 'starts'
      ? {
          executor: 'constant-arrival-rate',
          rate: 5,
          timeUnit: '1s',
          duration: '1m',
          preAllocatedVUs: 20,
          maxVUs: 100,
        }
      : mode === 'state'
        ? { executor: 'constant-vus', vus: 300, duration: '1m' }
        : {
            executor: 'constant-arrival-rate',
            rate: 50,
            timeUnit: '1s',
            duration: '1m',
            preAllocatedVUs: 100,
            maxVUs: 300,
          },
  },
  thresholds: {
    oge_5xx: ['rate<0.01'],
    ...(mode === 'state' ? { oge_session_read_ms: ['p(95)<500'] } : {}),
    ...(mode === 'save' ? { oge_answer_save_ms: ['p(95)<700'] } : {}),
    ...(mode === 'starts' ? { dropped_iterations: ['count==0'] } : {}),
  },
};

const fiveXx = new Rate('oge_5xx');
const readMs = new Trend('oge_session_read_ms', true);
const saveMs = new Trend('oge_answer_save_ms', true);
let vuState = null;

function request(method, path, body, token, tags = {}, extraHeaders = {}) {
  const params = {
    headers: { 'Content-Type': 'application/json', ...extraHeaders },
    tags,
  };
  if (token) params.headers.Authorization = `Bearer ${token}`;
  const res = http.request(method, `${base}/api/v1${path}`, body === null ? null : JSON.stringify(body), params);
  fiveXx.add(res.status >= 500);
  return res;
}

function initialize(index) {
  const login = `${prefix}${String(index).padStart(4, '0')}`;
  const auth = request('POST', '/auth/login', { login, password }, null, { operation: 'login' });
  if (!check(auth, { 'login succeeds': (r) => r.status === 200 })) fail(`Login ${login}: ${auth.status}`);
  const token = auth.json('access_token');
  const start = request('POST', `/exams/${examId}/sessions`, null, token, { operation: 'start' });
  if (!check(start, { 'session starts': (r) => r.status === 201 })) fail(`Start ${login}: ${start.status}`);
  const session = start.json();
  const task = session.tasks.find((item) => item.answerType !== 'ESSAY');
  return { token, sessionId: session.id, taskId: task ? task.taskId : null, revision: task && task.answer ? task.answer.revision : 0 };
}

export default function () {
  if (mode === 'starts') {
    // Each scheduled iteration uses a distinct preassigned student.
    const index = exec.scenario.iterationInTest + 1;
    const started = initialize(index);
    check(started, { 'start returned an id': (value) => Boolean(value.sessionId) });
    return;
  }

  if (vuState === null) vuState = initialize(exec.vu.idInTest);
  if (mode === 'state') {
    const res = request('GET', `/sessions/${vuState.sessionId}`, null, vuState.token, { operation: 'read' });
    readMs.add(res.timings.duration);
    check(res, { 'state succeeds': (r) => r.status === 200 && r.json('id') === vuState.sessionId });
    sleep(0.1);
    return;
  }

  if (!vuState.taskId) fail('The exam needs at least one objective task for save mode');
  const nextRevision = vuState.revision + 1;
  const res = request(
    'PUT',
    `/sessions/${vuState.sessionId}/answers/${vuState.taskId}`,
    { raw: String(nextRevision % 10), expectedRevision: vuState.revision },
    vuState.token,
    { operation: 'save' },
    { 'Idempotency-Key': `${prefix}-${exec.vu.idInTest}-${nextRevision}` },
  );
  saveMs.add(res.timings.duration);
  if (check(res, { 'save succeeds': (r) => r.status === 200 })) vuState.revision = res.json('revision');
}
