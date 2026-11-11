"use strict";

// Demo page. It has no payment logic of its own: everything it shows comes from the public
// API (POST/GET /payments, GET /alerts, GET /stats/providers). Data from the API is always
// inserted as text, never as HTML.

const AMOUNT_MINOR = 1999;
const COUNTRY = "DE";
const WATCH_MAX_SECONDS = 180;
const PROVIDERS_REFRESH_MS = 2000;

const SCENARIOS = [
  {
    token: "tok_approve",
    title: "Обычный платёж",
    what: "alpha сразу одобряет.",
    expect: "Одна попытка, оплачено через alpha.",
  },
  {
    token: "tok_soft_decline",
    title: "Мягкий отказ",
    what: "alpha отвечает «do not honor», код 05.",
    expect: "С картой всё может быть в порядке, поэтому сервис идёт к beta, и beta одобряет.",
  },
  {
    token: "tok_hard_decline",
    title: "Жёсткий отказ",
    what: "alpha отвечает «карта украдена», код 43.",
    expect:
      "Каскад останавливается на первой попытке: другой провайдер тут не поможет, " +
      "а повторы по такой карте вредны.",
  },
  {
    token: "tok_insufficient_funds",
    title: "Не хватает денег",
    what: "alpha отвечает «недостаточно средств», код 51.",
    expect: "Тоже стоп: у другого провайдера денег на карте больше не станет.",
  },
  {
    token: "tok_all_soft",
    title: "Все отказали мягко",
    what: "alpha, beta и gamma отвечают мягким отказом.",
    expect: "Три попытки, потом отказ по лимиту попыток.",
  },
  {
    token: "tok_timeout_approved",
    title: "Таймаут, но деньги списаны",
    what: "alpha не отвечает дольше 3 с, но платёж проводит.",
    expect:
      "К beta идти нельзя: можно списать дважды. Сервис переспрашивает alpha о статусе " +
      "и узнаёт, что деньги списаны. Одна попытка, одно списание.",
    wait: "alpha молчит. Сервис ждёт до 3 с, потом спросит статус.",
    watch: 8,
  },
  {
    token: "tok_timeout_not_processed",
    title: "Таймаут, платёж не проведён",
    what: "alpha не отвечает дольше 3 с и платёж не проводит.",
    expect:
      "Проверка статуса показывает, что alpha платёж не обрабатывал. " +
      "Тогда идти к beta безопасно.",
    wait: "alpha молчит. Сервис ждёт до 3 с, потом спросит статус.",
    watch: 8,
  },
  {
    token: "tok_beta_server_error",
    title: "Ошибка 500",
    what: "alpha мягко отказывает, beta отвечает 500, но деньги списывает.",
    expect:
      "Ответ 500 не говорит, списаны ли деньги. Сервис не идёт к gamma, " +
      "а переспрашивает beta и находит списание.",
    watch: 8,
  },
  {
    token: "tok_late_webhook",
    title: "Поздний вебхук",
    what: "alpha отвечает «не обработано», а через 3 с присылает вебхук «списано».",
    expect:
      "Платёж к этому времени уже оплачен через beta. Сервис замечает двойное списание, " +
      "возвращает деньги через alpha и пишет алерт.",
    watch: 20,
  },
  {
    token: "tok_timeout_stuck",
    title: "Зависший платёж",
    what: "alpha не отвечает и минуту держит платёж в обработке, вебхука нет.",
    expect:
      "За 15 с исход не выяснить, а к beta идти нельзя. Клиент получает «нужна проверка», " +
      "а примерно через минуту фоновая задача находит списание.",
    wait: "Сервис ждёт до 15 с: столько отведено на платёж.",
  },
  {
    kind: "idempotency",
    token: "tok_approve",
    title: "Повтор запроса",
    what: "Тот же запрос дважды с одним Idempotency-Key, потом тот же ключ с другим телом.",
    expect: "Второй ответ: тот же платёж, нового списания нет. Третий ответ: ошибка 422.",
  },
  {
    kind: "breaker",
    token: "tok_alpha_down",
    title: "Провайдер лежит",
    what: "12 платежей подряд, alpha на каждый отвечает техническим сбоем. Всегда в USD.",
    expect:
      "Когда за минуту набирается 10 запросов к alpha и не меньше половины из них сбои, " +
      "предохранитель выключает alpha на 30 с. Следующие платежи идут сразу в beta.",
  },
];

const PAYMENT_LABELS = {
  created: ["создан", "info"],
  processing: ["в процессе", "info"],
  succeeded: ["оплачен", "ok"],
  failed: ["отказ", "bad"],
  requires_review: ["нужна проверка", "warn"],
};

const ATTEMPT_LABELS = {
  pending: ["ждём ответ", "info"],
  approved: ["списано", "ok"],
  declined_soft: ["мягкий отказ", "warn"],
  declined_hard: ["жёсткий отказ", "bad"],
  error: ["сбой, не списано", "warn"],
  unknown: ["исход неизвестен", "info"],
  refund_pending: ["ждёт возврата", "bad"],
  refunded: ["возвращено", ""],
};

const OPEN_ATTEMPT_STATUSES = ["pending", "unknown", "refund_pending"];

// Runs whose cards are on screen and may still be sending requests or watching a payment.
const activeRuns = new Set();
// The scenario button whose payments the feed shows.
let activeButton = null;

// ---------------------------------------------------------------- helpers

const $ = (id) => document.getElementById(id);

function h(tag, props, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key.startsWith("on") && typeof value === "function") {
      node.addEventListener(key.slice(2), value);
    } else node.setAttribute(key, value === true ? "" : String(value));
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

const toneClass = (tone) => (tone ? `tone-${tone}` : "");
const fmtSeconds = (s, digits = 1) => s.toFixed(digits).replace(".", ",");
const fmtMoney = (minor, currency) => `${(minor / 100).toFixed(2).replace(".", ",")} ${currency}`;
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
// The API sends microseconds; not every browser parses more than three fractional digits.
const parseTime = (iso) => Date.parse(iso.replace(/(\.\d{3})\d+/, "$1"));
const secondsBetween = (fromIso, toIso) => (parseTime(toIso) - parseTime(fromIso)) / 1000;
const paymentLabel = (status) => PAYMENT_LABELS[status] || [status, ""];
const attemptLabel = (status) => ATTEMPT_LABELS[status] || [status, ""];

function randomKey() {
  const bytes = new Uint8Array(12);
  crypto.getRandomValues(bytes);
  return "demo-" + Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

async function api(method, path, body, headers = {}) {
  const init = { method, headers: { Accept: "application/json", ...headers } };
  if (body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  const response = await fetch(path, init);
  let data = null;
  try {
    data = await response.json();
  } catch {
    data = null;
  }
  return { status: response.status, headers: response.headers, data };
}

function errorText(result) {
  const detail = result.data && result.data.detail;
  if (detail && typeof detail === "object" && detail.code) return detail.code;
  if (detail) return JSON.stringify(detail);
  return `HTTP ${result.status}`;
}

function pay(token, { key = randomKey(), currency }) {
  const body = {
    customer_id: "demo_page",
    amount: AMOUNT_MINOR,
    currency,
    country: COUNTRY,
    payment_method: { type: "card", token },
  };
  return api("POST", "/payments", body, { "Idempotency-Key": key });
}

const selectedCurrency = () => $("currency").value;

// ---------------------------------------------------------------- explanations

function codeDetail(attempt) {
  if (!attempt.provider_code) return "";
  const message = attempt.provider_message ? `, «${attempt.provider_message}»` : "";
  return ` (код ${attempt.provider_code}${message})`;
}

function explainAttempt(a, payment) {
  const checked = a.status_checks > 0;
  // A late charge is refunded both when another attempt already paid and when the
  // customer was already told "failed".
  const why =
    payment.status === "failed"
      ? "клиенту уже ответили отказом"
      : "платёж уже оплачен другой попыткой";
  switch (a.status) {
    case "pending":
      return "Запрос у провайдера, ответа пока нет.";
    case "approved":
      if (a.late_approval) {
        return "Провайдер сначала ответил, что не списал, а потом сообщил, что всё-таки списал.";
      }
      return checked
        ? "Ответ провайдера не гарантировал результат (таймаут или 500), поэтому сервис " +
            "переспросил статус. Ответ: деньги списаны."
        : "Провайдер одобрил платёж.";
    case "declined_soft":
      return `Мягкий отказ${codeDetail(a)}. С картой всё может быть в порядке, поэтому можно попробовать другого провайдера.`;
    case "declined_hard":
      return `Жёсткий отказ${codeDetail(a)}. Другой провайдер не поможет, каскад остановлен.`;
    case "error":
      return checked
        ? "Ответ провайдера не гарантировал результат, поэтому сервис переспросил статус. " +
            "Ответ: платёж не обрабатывался, денег точно не взяли. Можно идти к следующему провайдеру."
        : `Технический сбой${codeDetail(a)}, провайдер гарантирует, что денег не взял. Можно идти к следующему провайдеру.`;
    case "unknown":
      return `Исход неизвестен${codeDetail(a)}. К следующему провайдеру идти нельзя: этот ещё может списать деньги. Сервис переспрашивает статус.`;
    case "refund_pending":
      return `Провайдер всё-таки списал деньги, но ${why}. Это лишнее списание, возврат поставлен в очередь.`;
    case "refunded":
      return `Лишнее списание: провайдер списал деньги, когда ${why}. Деньги возвращены.`;
    default:
      return a.status;
  }
}

function explainFailure(reason) {
  if (reason && reason.startsWith("hard_decline:")) {
    const [, provider, code] = reason.split(":");
    return `Отказ: жёсткий отказ у ${provider}, код ${code}.`;
  }
  const texts = {
    max_attempts_reached: "Отказ: исчерпан лимит попыток.",
    time_budget_exhausted: "Отказ: вышло время на платёж, новую попытку не начать.",
    no_available_provider: "Отказ: все провайдеры маршрута недоступны.",
    cascade_exhausted: "Отказ: провайдеры в маршруте закончились.",
    not_charged_after_review: "Отказ: проверка показала, что денег не брали.",
  };
  return texts[reason] || `Отказ: ${reason}.`;
}

function explainPayment(p) {
  switch (p.status) {
    case "succeeded":
      return `Оплачено через ${p.succeeded_provider}, попытка ${p.succeeded_attempt_no}.`;
    case "failed":
      return explainFailure(p.failure_reason);
    case "requires_review": {
      const budget = Math.round(secondsBetween(p.created_at, p.deadline_at));
      return (
        `Нужна проверка: ${budget} с на платёж прошли, а исход попытки неизвестен. ` +
        "Деньги могли списаться, поэтому сервис не идёт к другому провайдеру, " +
        "а продолжает спрашивать статус в фоне."
      );
    }
    default:
      return "Платёж в процессе.";
  }
}

// ---------------------------------------------------------------- run card

class Run {
  constructor(scenario) {
    this.t0 = performance.now();
    this.timer = null;
    // False once the card is removed: the run stops sending requests and watching.
    this.alive = true;
    activeRuns.add(this);
    this.badge = h("span", { class: "badge tone-info" }, "отправляем");
    this.statusLine = h("div", { class: "status-line" });
    this.extra = h("div");
    this.body = h("div");
    this.alertsBox = h("div", { class: "alerts" });
    this.logList = h("ol");
    this.foot = h("div", { class: "run-foot" });
    this.el = h(
      "article",
      { class: "run" },
      h(
        "div",
        { class: "run-head" },
        h("div", null, h("h3", null, scenario.title), h("code", null, scenario.token)),
        this.badge,
      ),
      h("p", { class: "expect" }, h("b", null, "Ожидаем. "), scenario.expect),
      this.statusLine,
      this.extra,
      this.body,
      this.alertsBox,
      h("details", { class: "log", open: true }, h("summary", null, "Журнал"), this.logList),
      this.foot,
    );
    $("runs").prepend(this.el);
    $("empty").hidden = true;
    // In the one-column layout the feed is below the buttons: bring the new card into view.
    if (window.matchMedia("(max-width: 900px)").matches) {
      this.el.scrollIntoView({ behavior: "smooth", block: "start" });
    }
  }

  elapsed() {
    return (performance.now() - this.t0) / 1000;
  }

  log(text, tone) {
    const time = h("span", { class: "t" }, `+${fmtSeconds(this.elapsed())} с`);
    this.logList.append(h("li", { class: tone || null }, time, text));
  }

  setBadge(text, tone) {
    this.badge.textContent = text;
    this.badge.className = `badge ${toneClass(tone)}`;
  }

  busy(text) {
    this.idle();
    const update = () =>
      this.statusLine.replaceChildren(
        h("span", { class: "spinner", "aria-hidden": "true" }),
        h("span", null, text()),
      );
    update();
    this.timer = setInterval(update, 200);
  }

  idle() {
    if (this.timer) clearInterval(this.timer);
    this.timer = null;
    this.statusLine.replaceChildren();
  }

  fail(text) {
    this.idle();
    this.setBadge("ошибка", "bad");
    this.log(text, "bad");
  }

  // The card was removed: stop the spinner and any further requests of this run.
  stop() {
    this.alive = false;
    this.idle();
    activeRuns.delete(this);
  }

  done() {
    activeRuns.delete(this);
  }
}

function renderAttempt(a, payment) {
  const [label, tone] = attemptLabel(a.status);
  const start = fmtSeconds(secondsBetween(payment.created_at, a.created_at), 2);
  const time = a.finished_at
    ? `+${start} … +${fmtSeconds(secondsBetween(payment.created_at, a.finished_at), 2)} с`
    : `с +${start} с`;
  const meta = [`проверок статуса: ${a.status_checks}`];
  if (a.provider_payment_id) meta.push(`id у провайдера: ${a.provider_payment_id}`);
  if (a.refund_id) meta.push(`возврат: ${a.refund_id}`);
  return h(
    "li",
    { class: `attempt ${toneClass(tone)}` },
    h(
      "div",
      { class: "attempt-head" },
      h("span", { class: "attempt-no" }, `Попытка ${a.attempt_no}`),
      h("b", null, a.provider),
      h("span", { class: `badge ${toneClass(tone)}` }, label),
      h("span", { class: "attempt-time", title: "По часам сервера от создания платежа" }, time),
    ),
    h("div", { class: "attempt-why" }, explainAttempt(a, payment)),
    h("div", { class: "attempt-meta" }, meta.join(" · ")),
  );
}

function renderPayment(run, p) {
  const [label, tone] = paymentLabel(p.status);
  run.setBadge(label, tone);
  run.body.replaceChildren(
    h("div", { class: "summary" }, explainPayment(p)),
    h(
      "div",
      { class: "route" },
      `${fmtMoney(p.amount, p.currency)}, маршрут по правилам из БД: ${p.route.join(" → ")}.`,
    ),
    h(
      "ol",
      { class: "attempts" },
      p.attempts.map((a) => renderAttempt(a, p)),
    ),
  );
  run.foot.replaceChildren(
    "Платёж ",
    h("code", null, p.id),
    " · ",
    h("a", { href: `/payments/${p.id}`, target: "_blank", rel: "noopener" }, "ответ API целиком"),
  );
}

// Log what changed between two reads of the same payment. Returns the number of changes.
function logDiff(run, before, after) {
  let changes = 0;
  for (const a of after.attempts) {
    const old = before.attempts.find((x) => x.attempt_no === a.attempt_no);
    if (!old) {
      run.log(`Попытка ${a.attempt_no}: ${a.provider}.`);
      changes += 1;
      continue;
    }
    if (a.status_checks > old.status_checks) {
      run.log(`Попытка ${a.attempt_no} (${a.provider}): проверка статуса, всего ${a.status_checks}.`);
      changes += 1;
    }
    if (a.status !== old.status) {
      const late = a.late_approval && !old.late_approval ? ", позднее одобрение" : "";
      const tone = a.status === "refunded" ? "good" : a.status === "refund_pending" ? "bad" : null;
      run.log(
        `Попытка ${a.attempt_no} (${a.provider}): «${attemptLabel(old.status)[0]}» → ` +
          `«${attemptLabel(a.status)[0]}»${late}.`,
        tone,
      );
      changes += 1;
    }
  }
  // The payment status last: it follows from what happened to the attempts.
  if (before.status !== after.status) {
    const tone = after.status === "succeeded" ? "good" : after.status === "failed" ? "bad" : null;
    run.log(
      `Платёж: «${paymentLabel(before.status)[0]}» → «${paymentLabel(after.status)[0]}».`,
      tone,
    );
    changes += 1;
  }
  return changes;
}

async function syncAlerts(run, paymentId, seen) {
  let result;
  try {
    result = await api("GET", `/alerts?payment_id=${encodeURIComponent(paymentId)}`);
  } catch {
    return 0; // the next poll asks again
  }
  if (!run.alive || result.status !== 200 || !Array.isArray(result.data)) return 0;
  let fresh = 0;
  for (const alert of result.data) {
    if (seen.has(alert.id)) continue;
    seen.add(alert.id);
    run.log(`Алерт ${alert.kind}: ${alert.message}`, "bad");
    fresh += 1;
  }
  run.alertsBox.replaceChildren(
    ...result.data.map((a) => h("div", { class: "alert" }, `Алерт ${a.kind}: ${a.message}`)),
  );
  return fresh;
}

const isSettled = (p) =>
  (p.status === "succeeded" || p.status === "failed") &&
  p.attempts.every((a) => !OPEN_ATTEMPT_STATUSES.includes(a.status));

// Keep reading the payment while background work may still change it.
async function follow(run, payment, minSeconds) {
  const seen = new Set();
  let current = payment;
  let changes = await syncAlerts(run, payment.id, seen);
  const start = performance.now();
  const waited = () => (performance.now() - start) / 1000;
  const done = () =>
    !run.alive ||
    (isSettled(current) && waited() >= minSeconds) ||
    waited() >= WATCH_MAX_SECONDS;
  if (done()) return;
  run.busy(() =>
    isSettled(current)
      ? `Следим за платежом ещё ${Math.max(0, Math.ceil(minSeconds - waited()))} с: может прийти поздний ответ провайдера.`
      : `Следим за платежом ${Math.floor(waited())} с: работу продолжает фоновая задача.`,
  );
  // The API may be down for a while (a restart, the crash test): keep asking, and log a
  // problem once, not on every poll.
  let problem = null;
  const report = (text) => {
    if (text !== problem) run.log(text, "bad");
    problem = text;
  };
  while (!done()) {
    await sleep(1000);
    if (!run.alive) return;
    let result;
    try {
      result = await api("GET", `/payments/${payment.id}`);
    } catch (error) {
      if (!run.alive) return;
      report(`API не отвечает (${error.message}). Страница продолжает спрашивать.`);
      continue;
    }
    if (!run.alive) return;
    if (result.status !== 200) {
      report(`GET /payments/{id}: HTTP ${result.status}. Страница продолжает спрашивать.`);
      continue;
    }
    if (problem) {
      run.log("API снова отвечает.", "good");
      problem = null;
    }
    changes += logDiff(run, current, result.data);
    current = result.data;
    renderPayment(run, current);
    changes += await syncAlerts(run, payment.id, seen);
  }
  run.idle();
  if (!run.alive) return;
  if (!isSettled(current)) {
    run.log(
      `Платёж не завершился за ${WATCH_MAX_SECONDS} с, страница перестала следить. Его состояние можно открыть по ссылке ниже.`,
      "bad",
    );
  } else if (changes === 0) {
    run.log("Наблюдение закончено: после ответа API ничего не изменилось.");
  } else {
    run.log("Наблюдение закончено: платёж завершён.");
  }
}

// ---------------------------------------------------------------- scenarios

async function runPayment(scenario, run) {
  const currency = selectedCurrency();
  run.log(`POST /payments: ${fmtMoney(AMOUNT_MINOR, currency)}, страна ${COUNTRY}, токен ${scenario.token}.`);
  run.busy(() => `Ждём ответ API: ${fmtSeconds(run.elapsed())} с. ${scenario.wait || ""}`);
  let result;
  try {
    result = await pay(scenario.token, { currency });
  } catch (error) {
    run.fail(`Запрос не дошёл до API: ${error.message}`);
    return;
  }
  if (!run.alive) return;
  run.idle();
  if (result.status !== 200 && result.status !== 201) {
    run.fail(`Ответ API: HTTP ${result.status}, ${errorText(result)}.`);
    return;
  }
  const p = result.data;
  const tone = p.status === "succeeded" ? "good" : p.status === "failed" ? "bad" : null;
  run.log(
    `Ответ API: HTTP ${result.status}, платёж «${paymentLabel(p.status)[0]}», попыток ${p.attempts.length}.`,
    tone,
  );
  renderPayment(run, p);
  await follow(run, p, scenario.watch || 0);
}

async function runIdempotency(scenario, run) {
  const currency = selectedCurrency();
  const key = randomKey();
  const checks = h("ul", { class: "checks" });
  run.extra.replaceChildren(checks);
  const check = (ok, text) => {
    checks.append(
      h("li", null, h("span", { class: ok ? "mark-ok" : "mark-bad" }, ok ? "✓ " : "✗ "), text),
    );
    return ok;
  };
  try {
    run.busy(() => "Первый запрос…");
    const first = await pay(scenario.token, { key, currency });
    if (!run.alive) return;
    run.log(`POST /payments с ключом ${key}: HTTP ${first.status}.`);
    const created = check(
      first.status === 201,
      `Первый запрос: HTTP ${first.status}, создан платёж ${first.data?.id}.`,
    );

    run.busy(() => "Тот же запрос ещё раз…");
    const second = await pay(scenario.token, { key, currency });
    if (!run.alive) return;
    const replayed = second.headers.get("Idempotent-Replayed");
    run.log(`Тот же запрос: HTTP ${second.status}, Idempotent-Replayed: ${replayed}.`);
    const same =
      second.status === 200 &&
      replayed === "true" &&
      second.data?.id === first.data?.id &&
      second.data?.attempts?.length === first.data?.attempts?.length;
    check(
      same,
      `Повтор: HTTP ${second.status}, заголовок Idempotent-Replayed: ${replayed}, ` +
        `попыток ${second.data?.attempts?.length}: ` +
        (same ? "тот же платёж, нового списания нет." : "ответ отличается от первого."),
    );

    run.busy(() => "Тот же ключ, другое тело…");
    const third = await pay("tok_soft_decline", { key, currency });
    if (!run.alive) return;
    run.log(`Тот же ключ с токеном tok_soft_decline: HTTP ${third.status}, ${errorText(third)}.`);
    const rejected = check(
      third.status === 422 && errorText(third) === "idempotency_key_reused",
      `Тот же ключ, другое тело: HTTP ${third.status}, ${errorText(third)}. ` +
        "Сервис не угадывает, какой запрос правильный, а отклоняет второй.",
    );

    run.idle();
    if (created) renderPayment(run, first.data);
    const allOk = created && same && rejected;
    run.setBadge(allOk ? "всё сходится" : "не сходится", allOk ? "ok" : "bad");
  } catch (error) {
    run.fail(`Запрос не дошёл до API: ${error.message}`);
  }
}

async function runBreaker(scenario, run) {
  const total = 12;
  const series = h("ul", { class: "series" });
  run.extra.replaceChildren(
    h(
      "p",
      { class: "series-intro" },
      `${total} отдельных платежей по ${fmtMoney(AMOUNT_MINOR, "USD")}, по строке на каждый. ` +
        "В строке попытки этого платежа по порядку.",
    ),
    series,
  );
  let firstSkipped = null;
  let oneCharge = 0;
  try {
    for (let i = 1; i <= total; i += 1) {
      run.busy(() => `Платёж ${i} из ${total}…`);
      const result = await pay(scenario.token, { currency: "USD" });
      if (!run.alive) return;
      const label = h("b", null, `Платёж ${i}: `);
      if (!result.data || !Array.isArray(result.data.attempts)) {
        series.append(h("li", null, label, `HTTP ${result.status}, ${errorText(result)}`));
        continue;
      }
      const attempts = result.data.attempts;
      series.append(
        h(
          "li",
          null,
          label,
          attempts.map((a) => `${a.provider}: ${attemptLabel(a.status)[0]}`).join(" → "),
        ),
      );
      if (attempts.filter((a) => a.status === "approved").length === 1) oneCharge += 1;
      const first = attempts.length ? attempts[0].provider : null;
      if (firstSkipped === null && first && first !== "mock_alpha") {
        firstSkipped = i;
        run.log(`Платёж ${i} начался сразу с ${first}: alpha пропущен.`, "good");
      }
    }
    run.idle();
    const stats = await api("GET", "/stats/providers");
    if (!run.alive) return;
    const alpha = Array.isArray(stats.data)
      ? stats.data.find((s) => s.provider === "mock_alpha")
      : null;
    const circuit = (alpha && alpha.circuit) || {};
    const open = circuit.state === "open";
    let text;
    // The breaker counts every request to alpha in the last minute, including those of
    // earlier scenarios: their failures bring the trip closer, their successes delay it.
    const windowNote =
      " В минутном окне предохранителя считаются и запросы к alpha из предыдущих " +
      "сценариев: сбои среди них приближают срабатывание, удачные откладывают.";
    if (firstSkipped === 1) {
      text = "alpha был выключен предохранителем ещё до начала серии, поэтому серия сразу пошла в beta.";
    } else if (firstSkipped !== null) {
      text =
        `Первые ${firstSkipped - 1} платежей начинались с alpha и после сбоя уходили в beta. ` +
        `С платежа ${firstSkipped} alpha пропускается: предохранитель его выключил.`;
      if (firstSkipped - 1 !== 10) text += windowNote;
    } else {
      text =
        "Предохранитель не сработал: все платежи начинались с alpha." +
        windowNote +
        " Подождите минуту и запустите серию снова.";
    }
    if (open) text += ` alpha будет выключен ещё ${circuit.retry_after_seconds} с, потом снова получит запросы.`;
    text +=
      oneCharge === total
        ? ` В каждом из ${total} платежей ровно одно списание.`
        : ` Платежей ровно с одним списанием: ${oneCharge} из ${total}.`;
    run.body.replaceChildren(h("div", { class: "summary" }, text));
    run.setBadge(open ? "alpha выключен" : "не сработал", open ? "warn" : "bad");
    run.log(`Предохранитель alpha: ${circuit.state || "нет данных"}.`, open ? "good" : "bad");
  } catch (error) {
    run.fail(`Запрос не дошёл до API: ${error.message}`);
  }
}

function start(scenario) {
  const runners = { idempotency: runIdempotency, breaker: runBreaker };
  const runner = runners[scenario.kind] || runPayment;
  const run = new Run(scenario);
  runner(scenario, run)
    .catch((error) => {
      // Last line of defence: never leave a card spinning without an explanation.
      console.error(error);
      if (run.alive) run.fail(`Сбой на странице: ${error.message}`);
    })
    .finally(() => run.done());
}

// Remove every card and stop the runs behind them. Payments already sent are not
// affected: the service finishes them anyway, the page just stops watching.
function clearFeed() {
  for (const run of [...activeRuns]) run.stop();
  $("runs").replaceChildren();
  $("empty").hidden = false;
}

function setActiveButton(button) {
  if (activeButton) {
    activeButton.classList.remove("active");
    activeButton.setAttribute("aria-pressed", "false");
  }
  activeButton = button;
  if (button) {
    button.classList.add("active");
    button.setAttribute("aria-pressed", "true");
  }
}

// Another scenario clears the feed; the same one again adds a payment on top.
function selectScenario(button, scenario) {
  if (button !== activeButton) {
    clearFeed();
    setActiveButton(button);
  }
  start(scenario);
}

// ---------------------------------------------------------------- side panels

function renderProvider(s) {
  const circuit = s.circuit || {};
  const [label, tone] =
    circuit.state === "open"
      ? [`выключен, ещё ${circuit.retry_after_seconds} с`, "bad"]
      : circuit.state === "closed"
        ? ["работает", "ok"]
        : ["состояние неизвестно", ""];
  const pct = (x) => (x === null || x === undefined ? "нет данных" : `${Math.round(x * 100)}%`);
  return h(
    "div",
    { class: "provider" },
    h(
      "div",
      { class: "provider-head" },
      h("b", null, s.provider),
      h("span", { class: `badge ${toneClass(tone)}` }, label),
    ),
    h(
      "div",
      { class: "provider-stats" },
      `Попыток ${s.attempts}: одобрено ${s.approved}, мягких отказов ${s.declined_soft}, ` +
        `жёстких ${s.declined_hard}, сбоев ${s.errors}, неизвестно ${s.unknown}.`,
    ),
    h(
      "div",
      { class: "provider-stats" },
      `Одобряемость ${pct(s.approval_rate)}, технические сбои ${pct(s.technical_failure_rate)}.`,
    ),
  );
}

function setBanner(text) {
  const banner = $("banner");
  banner.hidden = !text;
  banner.textContent = text || "";
}

async function refreshProviders() {
  const box = $("providers");
  let result = null;
  try {
    result = await api("GET", "/stats/providers");
  } catch {
    result = null;
  }
  if (!result || result.status !== 200 || !Array.isArray(result.data)) {
    box.replaceChildren(h("p", { class: "hint" }, "API не отвечает."));
    setBanner(null);
    return;
  }
  box.replaceChildren(...result.data.map(renderProvider));
  const open = result.data.filter((s) => s.circuit && s.circuit.state === "open");
  setBanner(
    open.length
      ? `Предохранитель сейчас выключил ${open.map((s) => s.provider).join(", ")} ` +
          `(ещё ${Math.max(...open.map((s) => s.circuit.retry_after_seconds))} с). Пока он выключен, ` +
          "платежи идут мимо этого провайдера, и сценарии ведут себя не так, как описано."
      : null,
  );
}

function renderCurrencyHint() {
  $("currency-hint").textContent =
    selectedCurrency() === "USD"
      ? "Маршрут по умолчанию: alpha → beta → gamma. Описания сценариев даны для него."
      : "Для EUR в БД своё правило: beta → alpha → gamma. Те же токены пойдут другим путём: " +
        "так видно, что маршрут берётся из правил, а не зашит в код.";
}

function init() {
  $("scenarios").replaceChildren(
    ...SCENARIOS.map((s) =>
      h(
        "button",
        {
          type: "button",
          class: "scenario",
          "aria-pressed": "false",
          onclick: (event) => selectScenario(event.currentTarget, s),
        },
        h("b", null, s.title),
        h("span", null, s.what),
      ),
    ),
  );
  $("currency").addEventListener("change", renderCurrencyHint);
  $("clear").addEventListener("click", () => {
    clearFeed();
    setActiveButton(null);
  });
  renderCurrencyHint();
  refreshProviders();
  setInterval(refreshProviders, PROVIDERS_REFRESH_MS);
}

init();
