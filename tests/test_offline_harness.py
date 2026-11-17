"""Offline harness: drives app.serve() through a real h2o_wave Query (AsyncPage.save stubbed,
llm_chat mocked) to catch API misuse and logic bugs without a Wave server or network."""
import asyncio
import sys
import time
import traceback

import h2o_wave.core as core
import h2o_wave.server as server

sys.path.insert(0, ".")
import app as appmod  # noqa: E402


async def _fake_save(self):
    self._diff()

core.AsyncPage.save = _fake_save

LLM_CALLS = []
LLM_MODE = {"mode": "ok"}


def fake_llm_chat(cfg, messages, max_tokens=300, temperature=0.2, timeout=25):
    LLM_CALLS.append((cfg["provider"], cfg["model"], messages))
    if LLM_MODE["mode"] == "429":
        return None, "rate limit reached - free models are limited per minute and per day (429)"
    if LLM_MODE["mode"] == "empty":
        return None, "empty response from model"
    return f"AI says: {messages[-1]['content'][:30]}", None

REAL_LLM_CHAT = appmod.llm_chat
appmod.llm_chat = fake_llm_chat

TG_SENT = []
TG_MODE = {"ok": True}


def fake_tg_send(token, chat_id, text):
    TG_SENT.append((token, str(chat_id), text))
    return (True, None) if TG_MODE["ok"] else (False, "chat not found - open your bot in Telegram and press Start first (400)")


def fake_tg_detect(token):
    return ("555111222", "Erwin", None)


appmod.tg_send_blocking = fake_tg_send
appmod.tg_detect_blocking = fake_tg_detect


def make_q(client=None, **args):
    q = server.Query(site=core.AsyncSite(), mode=server.UNICAST, auth=None, client_id="t", route="/",
                     app_state=core.Expando(), user_state=core.Expando(),
                     client_state=client or core.Expando(), args=core.Expando(),
                     events=core.Expando(), headers={})
    sub = args.pop("_submit", None)
    for k, v in args.items():
        q.args[k] = v
    if sub:
        q.args["__wave_submission_name__"] = sub
    return q


errors = []


def check(name, cond, detail=""):
    if cond:
        print(f"[OK] {name}")
    else:
        errors.append(name)
        print(f"[FAIL] {name} {detail}")


async def step(name, coro):
    try:
        await coro
        print(f"[OK] {name}")
    except Exception as e:
        errors.append(name)
        print(f"[FAIL] {name}: {type(e).__name__}: {e}")
        traceback.print_exc()


async def main():
    q = make_q()
    await step("initial load", appmod.serve(q))
    c = q.client

    for tab in ["alerts", "health", "copilot", "settings", "overview"]:
        qq = make_q(c, **{"#": tab, "_submit": "#"})
        await step(f"tab #{tab}", appmod.serve(qq))
        check(f"active_tab=#{tab}", c.active_tab == "#" + tab, c.active_tab)

    # --- simulation mode: inject, copilot rule-based, no LLM calls ---------------------
    qq = make_q(c, inject_machine="hyd-03", inject_sensor="pressure", inject_anomaly=True, _submit="inject_anomaly")
    await step("inject (simulation)", appmod.serve(qq))
    check("alert created", len(c.alerts) == 1 and len(c.actions) == 1, f"{len(c.alerts)}/{len(c.actions)}")
    check("only hyd-03 touched", c.machines["hyd-03"]["status"] == "Critical"
          and all(m["status"] == "Normal" for k, m in c.machines.items() if k != "hyd-03"))
    check("no LLM call in simulation", len(LLM_CALLS) == 0)

    qq = make_q(c, copilot_input="Which machine should we inspect first?", copilot_send=True, _submit="copilot_send")
    await step("copilot send (simulation)", appmod.serve(qq))
    check("chat has 2 msgs", len(c.chat) == 2 and c.chat[-1]["role"] == "assistant", str(c.chat))
    check("rule-based mentions Press", "Hydraulic Press #3" in c.chat[-1]["text"], c.chat[-1]["text"])
    check("src is rule-based", "Rule-based" in c.chat[-1]["src"])

    qq = make_q(c, copilot_suggest="Which machine is most likely to fail next?", _submit="copilot_suggest")
    await step("copilot suggestion", appmod.serve(qq))
    check("forecast answer", "Failure forecast" in c.chat[-1]["text"] and "Hydraulic Press #3" in c.chat[-1]["text"], c.chat[-1]["text"])

    qq = make_q(c, ask_ai_fleet=True, _submit="ask_ai_fleet")
    await step("ask AI (fleet)", appmod.serve(qq))
    check("switched to copilot tab", c.active_tab == "#copilot")

    qq = make_q(c, copilot_clear=True, _submit="copilot_clear")
    await step("clear chat", appmod.serve(qq))
    check("chat cleared", c.chat == [])

    # --- work orders & engineer dispatch ------------------------------------------------
    wo = c.actions[0]
    check("WO starts Open + unassigned", wo["state"] == "Open" and wo["assignee"] is None and wo["priority"] == "P1 - Critical", str(wo))
    check("WO has reading/limit", wo["sensor"] == "Pressure" and wo["reading"] and wo["limit"])
    check("engineers loaded", len(c.engineers) == 10 and sum(e["on_duty"] for e in c.engineers) == 6)
    qq = make_q(c)
    rec = appmod.recommend_engineer(qq, wo)
    check("recommended = on-duty Hydraulics", rec["team"] == "Hydraulics Team" and rec["on_duty"], str(rec))
    check("off-duty never offered", all(e["on_duty"] for e in appmod.on_duty_engineers(qq, wo)))

    qq = make_q(c, open_wo=wo["id"], _submit="open_wo")
    await step("open WO from banner", appmod.serve(qq))
    check("dialog open on alerts tab", c.dialog_wo == wo["id"] and c.active_tab == "#alerts")
    check("dialog built", appmod.wo_dialog(qq) is not None and len(appmod.wo_dialog(qq).items) >= 5)

    qq = make_q(c, wo_assign=wo["id"], engineer="eng-03", _submit="wo_assign")      # off-duty engineer
    await step("assign off-duty engineer", appmod.serve(qq))
    check("off-duty rejected", wo["state"] == "Open" and wo["assignee"] is None)

    qq = make_q(c, wo_table=[wo["id"]], _submit="wo_table")
    await step("click WO row", appmod.serve(qq))
    check("row click opens dialog", c.dialog_wo == wo["id"])
    qq = make_q(c, wo_assign=wo["id"], engineer="eng-05", _submit="wo_assign")
    await step("assign Slamet", appmod.serve(qq))
    check("assigned", wo["state"] == "Assigned" and wo["assignee"] == "eng-05" and c.dialog_wo is None)
    check("workload counted", appmod.engineer_load(qq, "eng-05") == 1)
    check("flash set", c.wo_flash and "Slamet Riyadi" in c.wo_flash[1])
    qq = make_q(c, wo_start=wo["id"], _submit="wo_start")
    await step("start work", appmod.serve(qq))
    check("in progress", wo["state"] == "In Progress")
    qq = make_q(c, wo_complete=wo["id"], _submit="wo_complete")
    await step("complete WO", appmod.serve(qq))
    check("completed", wo["state"] == "Completed" and appmod.engineer_load(qq, "eng-05") == 0)

    qq = make_q(c, roster_table=["eng-03"], _submit="roster_table")
    await step("roster toggle on", appmod.serve(qq))
    check("Rizky now on duty", appmod.find_engineer(qq, "eng-03")["on_duty"] is True)
    qq = make_q(c, roster_table=["eng-03"], _submit="roster_table")
    await step("roster toggle off", appmod.serve(qq))

    qq = make_q(c, wo_table=[wo["id"]], _submit="wo_table")
    await step("reopen completed WO", appmod.serve(qq))
    check("completed dialog opens", c.dialog_wo == wo["id"])
    qq = make_q(c)
    qq.events["wo_dialog"] = core.Expando({"dismissed": True})
    await step("dialog dismissed (X)", appmod.serve(qq))
    check("dismiss clears dialog", c.dialog_wo is None)

    qq = make_q(c, inject_machine="arm-04", inject_sensor="temperature", inject_anomaly=True, _submit="inject_anomaly")
    await step("second fault", appmod.serve(qq))
    check("new WO is Open with a unique id", len(c.actions) == 2 and c.actions[0]["state"] == "Open"
          and c.actions[0]["id"] != wo["id"])
    for tab in ["overview", "settings", "copilot", "alerts"]:
        qq = make_q(c, **{"#": tab, "_submit": "#"})
        await step(f"banner path #{tab}", appmod.serve(qq))
    qq = make_q(c, copilot_input="Who is on duty?", copilot_send=True, _submit="copilot_send")
    await step("copilot on-duty", appmod.serve(qq))
    check("on-duty answer lists engineers", "Budi Santoso" in c.chat[-1]["text"], c.chat[-1]["text"])
    c.chat = []
    c.alerts.clear()
    c.actions.clear()

    # --- predictive health: forecast, risk, drift, downtime avoided -----------------------
    qq = make_q(c)
    fc = appmod.fleet_forecast(qq)
    check("forecast covers all machines", len(fc) == 5)
    check("critical machine ranked first, high risk", fc[0]["id"] == "hyd-03" and fc[0]["risk"] == "High", str(fc[0]))
    check("health in range", all(0 <= f["health"] <= 100 for f in fc))
    others = [f for f in fc if f["status"] == "Normal"]
    check("healthy machines are low risk", others and all(f["risk"] == "Low" for f in others), str(others))
    check("nothing avoided before any completion", appmod.downtime_avoided(qq) == 0)
    check("rupiah format", appmod.fmt_rupiah(180_000_000) == "Rp 180 jt" and appmod.fmt_rupiah(1_200_000_000) == "Rp 1,2 M")

    # gradual degradation: forecast must warn BEFORE the alert fires
    c.alerts.clear(); c.actions.clear()
    appmod.random.seed(7)
    qq = make_q(c, inject_machine="comp-05", inject_sensor="temperature", start_drift=True, _submit="start_drift")
    await step("start drift", appmod.serve(qq))
    c.running = False                                           # drive ticks manually, no background loop
    check("drift registered", c.drift == {"comp-05": "temperature"} and c.active_tab == "#health")
    seen_forecast_first, alerted_at = False, None
    for i in range(40):
        appmod.apply_tick(qq)
        f = [x for x in appmod.fleet_forecast(qq) if x["id"] == "comp-05"][0]
        if alerted_at is None and f["eta"] is not None and f["status"] == "Normal":
            seen_forecast_first = True
        if c.alerts and alerted_at is None:
            alerted_at = i
    check("forecast flagged the drift while still Normal", seen_forecast_first)
    check("warning then critical alerts for the drifting sensor", {a["severity"] for a in c.alerts} == {"Warning", "Critical"},
          str([a["severity"] for a in c.alerts]))
    check("drift stops itself at Critical", c.drift == {})
    check("two work orders (escalation)", len(c.actions) == 2 and c.actions[0]["priority"] == "P1 - Critical")

    qq = make_q(c, **{"#": "health", "_submit": "#"})
    await step("render health tab", appmod.serve(qq))
    qq = make_q(c, ask_ai_risk=True, _submit="ask_ai_risk")
    await step("explain with AI (simulation)", appmod.serve(qq))
    check("risk question answered", "Failure forecast" in c.chat[-1]["text"], c.chat[-1]["text"])
    c.chat = []
    # completing a work order books avoided downtime
    wo = c.actions[0]
    qq = make_q(c, wo_assign=wo["id"], engineer="eng-01", _submit="wo_assign")
    await step("assign", appmod.serve(qq))
    qq = make_q(c, wo_complete=wo["id"], _submit="wo_complete")
    await step("complete", appmod.serve(qq))
    check("downtime avoided counted", appmod.downtime_avoided(qq) == 35_000_000 * 4, str(appmod.downtime_avoided(qq)))
    qq = make_q(c, stop_drift=True, _submit="stop_drift")
    await step("stop drift", appmod.serve(qq))
    c.alerts.clear(); c.actions.clear()
    for m in c.machines.values():
        for sst in m["sensors"].values():
            sst["level"] = 0; sst["active_fault"] = False

    # --- OpenRouter without a key -> graceful fallback ----------------------------------
    qq = make_q(c, ai_provider="openrouter", _submit="ai_provider")
    await step("provider -> openrouter", appmod.serve(qq))
    check("provider set", c.ai_provider == "openrouter")
    check("no key -> not ready", not appmod.ai_ready(qq))
    qq = make_q(c, copilot_input="status?", copilot_send=True, _submit="copilot_send")
    await step("copilot without key", appmod.serve(qq))
    check("hint about missing key", "No API key" in c.chat[-1]["text"], c.chat[-1]["text"])
    check("still no LLM call", len(LLM_CALLS) == 0)

    # --- OpenRouter with key: alerts + copilot use the LLM ------------------------------
    qq = make_q(c, openrouter_key="sk-or-test", openrouter_model="openrouter/free", save_ai=True, _submit="save_ai")
    await step("save key", appmod.serve(qq))
    check("ai ready", appmod.ai_ready(qq))
    c.active_tab = "#settings"          # the Test button lives on the Settings tab
    n0 = len(LLM_CALLS)
    qq = make_q(c, ai_test=True, openrouter_key="sk-or-test", openrouter_model="openrouter/free", _submit="ai_test")
    await step("test connection", appmod.serve(qq))
    check("test made one LLM call", len(LLM_CALLS) == n0 + 1)
    check("test success flash consumed", c.ai_flash is None)

    qq = make_q(c, inject_machine="cnc-01", inject_sensor="temperature", inject_anomaly=True, _submit="inject_anomaly")
    await step("inject with AI", appmod.serve(qq))
    check("alert is AI-written", c.alerts[0]["ai"] == "OpenRouter" and c.alerts[0]["message"].startswith("AI says"),
          str(c.alerts[0]["message"]))
    check("LLM got OpenRouter model", any(p == "openrouter" and m == "openrouter/free" for p, m, _ in LLM_CALLS))

    qq = make_q(c, copilot_input="Explain the latest alert", copilot_send=True, _submit="copilot_send")
    await step("copilot with AI", appmod.serve(qq))
    check("copilot AI answer", c.chat[-1]["text"].startswith("AI says"), c.chat[-1]["text"])
    sysmsg = [m for p, mo, ms in LLM_CALLS for m in ms if m["role"] == "system" and "FLEET SNAPSHOT" in m["content"]]
    check("snapshot sent to LLM", bool(sysmsg) and "Hydraulic Press #3" in sysmsg[-1]["content"])
    check("api key never in prompts", all("sk-or-test" not in str(ms) for _, _, ms in LLM_CALLS))

    # --- rate limit: fallback + cool-down ---------------------------------------------
    LLM_MODE["mode"] = "429"
    before = len(LLM_CALLS)
    qq = make_q(c, copilot_input="status", copilot_send=True, _submit="copilot_send")
    await step("copilot on 429", appmod.serve(qq))
    check("fallback note shown", "AI unavailable" in c.chat[-1]["text"], c.chat[-1]["text"])
    check("cool-down active", c.ai_cooldown_until > time.time())
    qq = make_q(c, copilot_input="status again", copilot_send=True, _submit="copilot_send")
    await step("copilot during cool-down", appmod.serve(qq))
    check("no extra LLM call during cool-down", len(LLM_CALLS) == before + 1, f"{len(LLM_CALLS)-before}")
    check("cool-down message", "cooling down" in c.chat[-1]["text"], c.chat[-1]["text"])
    LLM_MODE["mode"] = "ok"
    c.ai_cooldown_until = 0

    # --- custom provider ----------------------------------------------------------------
    qq = make_q(c, ai_provider="custom", llm_base_url="https://x/v1", llm_model="m", llm_api_key="", _submit="ai_provider")
    await step("provider -> custom", appmod.serve(qq))
    check("custom ready", appmod.ai_ready(qq) and appmod.ai_config(qq)["provider"] == "custom")

    qq = make_q(c, ai_provider="custom", llm_base_url="https://mlis.imid.local", llm_model="qwen3-coder-30b",
                llm_api_key="", llm_verify_ssl=False, _submit="ai_provider")
    await step("custom MLIS cfg", appmod.serve(qq))
    cfg = appmod.ai_config(qq)
    check("/v1 appended", cfg["base"] == "https://mlis.imid.local/v1", cfg["base"])
    check("verify off", cfg["verify"] is False)

    # --- Telegram notifications -----------------------------------------------------------
    qq = make_q(c, reset_sim=True, _submit="reset_sim")
    await step("reset before telegram tests", appmod.serve(qq))
    c.ai_provider = "simulation"
    GOOD_TOKEN = "123456789:AAH-abcdefghijklmnopqrstuvwxyz012345"
    check("telegram off by default", not appmod.tg_ready(qq))
    qq = make_q(c, inject_machine="hyd-03", inject_sensor="pressure", inject_anomaly=True, _submit="inject_anomaly")
    await step("inject with telegram off", appmod.serve(qq))
    check("nothing sent while off", TG_SENT == [])
    c.alerts.clear(); c.actions.clear()
    for m in c.machines.values():
        for sst in m["sensors"].values():
            sst["level"] = 0; sst["active_fault"] = False

    qq = make_q(c, tg_token="not-a-token", tg_chat_id="12", tg_save=True, _submit="tg_save")
    await step("save invalid telegram settings", appmod.serve(qq))
    check("invalid token/chat rejected", c.tg_token == "" and c.tg_chat_id == "" and c.tg_flash and c.tg_flash[0] == "warning")
    qq = make_q(c, tg_token=GOOD_TOKEN, tg_detect=True, _submit="tg_detect")
    await step("detect chat id", appmod.serve(qq))
    check("chat id detected", c.tg_chat_id == "555111222" and c.tg_token == GOOD_TOKEN)
    qq = make_q(c, tg_token=GOOD_TOKEN, tg_chat_id="555111222", tg_lang="id", tg_min_sev="Warning",
                tg_on_alert=True, tg_on_assign=True, tg_on_progress=True, tg_on_predict=True, tg_test=True, _submit="tg_test")
    await step("send test message", appmod.serve(qq))
    check("test message sent + enabled", len(TG_SENT) == 1 and c.tg_enabled and "Tes notifikasi" in TG_SENT[0][2], str(TG_SENT))
    TG_SENT.clear()

    qq = make_q(c, inject_machine="hyd-03", inject_sensor="pressure", inject_anomaly=True, _submit="inject_anomaly")
    await step("inject with telegram on", appmod.serve(qq))
    check("alert message sent", len(TG_SENT) == 1 and TG_SENT[0][1] == "555111222", str(TG_SENT))
    msg = TG_SENT[0][2]
    wo = c.actions[0]
    check("alert message content", wo["id"] in msg and "Hydraulic Press #3" in msg and "CRITICAL" in msg and "menunggu engineer" in msg, msg)
    TG_SENT.clear()

    qq = make_q(c, wo_assign=wo["id"], engineer="eng-05", _submit="wo_assign")
    await step("assign (telegram)", appmod.serve(qq))
    await asyncio.sleep(0.05)
    check("assignment message sent", len(TG_SENT) == 1 and "Slamet Riyadi" in TG_SENT[0][2] and "ditugaskan" in TG_SENT[0][2], str(TG_SENT))
    TG_SENT.clear()
    c.engineers[4]["telegram_chat_id"] = "777888999"          # Slamet has his own Telegram
    wo2 = {**wo, "state": "Open", "assignee": None, "id": "WO-9"}
    c.actions.appendleft(wo2)
    qq = make_q(c, wo_assign="WO-9", engineer="eng-05", _submit="wo_assign")
    await step("assign to engineer with own chat", appmod.serve(qq))
    await asyncio.sleep(0.05)
    check("engineer + supervisor both notified", sorted(t[1] for t in TG_SENT) == ["555111222", "777888999"], str(TG_SENT))
    TG_SENT.clear()
    qq = make_q(c, wo_start=wo["id"], _submit="wo_start")
    await step("start (telegram)", appmod.serve(qq))
    qq = make_q(c, wo_complete=wo["id"], _submit="wo_complete")
    await step("complete (telegram)", appmod.serve(qq))
    await asyncio.sleep(0.05)
    check("progress messages sent", len(TG_SENT) == 2 and "selesai" in TG_SENT[1][2], str(TG_SENT))
    TG_SENT.clear()

    # severity filter + English + escaping
    c.tg_min_sev, c.tg_lang = "Critical", "en"
    fake = [{"severity": "Warning", "machine": "A<b>", "message": "x & y", "ctx": {"sensor": "Temp"}, "wo": "WO-9"}]
    await appmod.notify_new_alerts(qq, fake)
    check("warning filtered when Critical only", TG_SENT == [])
    c.tg_min_sev = "Warning"
    await appmod.notify_new_alerts(qq, fake)
    check("english + HTML escaped", len(TG_SENT) == 1 and "waiting for an engineer" in TG_SENT[0][2]
          and "A&lt;b&gt;" in TG_SENT[0][2] and "x &amp; y" in TG_SENT[0][2], str(TG_SENT))
    TG_SENT.clear()
    burst = [{"severity": "Critical", "machine": f"M{i}", "message": "m", "ctx": {"sensor": "T"}, "wo": "WO-9"} for i in range(6)]
    await appmod.notify_new_alerts(qq, burst)
    check("burst capped (3 + summary)", len(TG_SENT) == appmod.MAX_TG_PER_TICK + 1, str(len(TG_SENT)))
    TG_SENT.clear()
    c.tg_lang = "id"

    # early warning fires once, before the alert
    c.alerts.clear(); c.actions.clear()
    for m in c.machines.values():
        for sst in m["sensors"].values():
            sst["level"] = 0; sst["active_fault"] = False
    appmod.random.seed(11)
    qq = make_q(c, inject_machine="comp-05", inject_sensor="temperature", start_drift=True, _submit="start_drift")
    await step("start drift (telegram)", appmod.serve(qq))
    c.running = False
    for _ in range(40):
        appmod.apply_tick(qq)
        await appmod.notify_predictions(qq)
    preds = [t for t in TG_SENT if "Peringatan dini" in t[2]]
    check("early warning sent exactly once", len(preds) == 1, str(len(preds)))
    TG_SENT.clear(); c.drift = {}

    # failures never crash and never leak the token
    TG_MODE["ok"] = False
    await appmod.tg_notify(qq, "hello")
    check("error recorded", c.tg_last_error and GOOD_TOKEN not in c.tg_last_error, str(c.tg_last_error))
    qq = make_q(c, tg_token=GOOD_TOKEN, tg_chat_id="555111222", tg_test=True, _submit="tg_test")
    await step("test message failure", appmod.serve(qq))
    TG_MODE["ok"] = True
    qq = make_q(c, tg_enabled=False, _submit="tg_enabled")
    await step("toggle off", appmod.serve(qq))
    check("telegram disabled", not appmod.tg_ready(qq))
    TG_SENT.clear()
    await appmod.notify_new_alerts(qq, burst)
    check("nothing sent when disabled", TG_SENT == [])
    c.alerts.clear(); c.actions.clear()
    c.active_tab = "#settings"
    qq = make_q(c, **{"#": "settings", "_submit": "#"})
    await step("settings renders telegram card", appmod.serve(qq))

    # --- real llm_chat parsing (requests.post mocked) --------------------------------------
    class FakeResp:
        def __init__(self, status=200, body=None):
            self.status_code, self._b = status, body
        def json(self):
            if isinstance(self._b, Exception):
                raise self._b
            return self._b

    def completion(content, finish="stop"):
        return FakeResp(200, {"choices": [{"message": {"content": content}, "finish_reason": finish}]})

    calls = []
    def run_llm(responses, provider="openrouter"):
        calls.clear()
        it = iter(responses)
        def fake_post(url, headers=None, json=None, timeout=None, verify=True):
            calls.append(json)
            return next(it)
        real_post = appmod.requests.post
        appmod.requests.post = fake_post
        try:
            return REAL_LLM_CHAT(dict(provider=provider, base="https://x/v1", key="k", model="m"), [{"role": "user", "content": "hi"}], 20)
        finally:
            appmod.requests.post = real_post

    check("llm ok", run_llm([completion("OK")]) == ("OK", None))
    check("llm strips <think>", run_llm([completion("<think>hmm</think>Hello")]) == ("Hello", None))
    t, e = run_llm([completion(None, "length"), completion("Recovered")])
    check("empty+length retries with bigger budget", t == "Recovered" and len(calls) == 2 and calls[1]["max_tokens"] > calls[0]["max_tokens"], str(calls))
    t, e = run_llm([completion(None, "length"), completion("", "length")])
    check("still empty -> helpful error", t is None and "thinking" in e, str(e))
    t, e = run_llm([completion("", "stop")])
    check("empty stop -> no retry, clear error", t is None and len(calls) == 1 and "busy" in e, str(e))
    t, e = run_llm([FakeResp(200, {"error": {"message": "upstream down"}})])
    check("200 + error body handled", t is None and "upstream down" in e, str(e))
    t, e = run_llm([completion([{"type": "text", "text": "Part"}, {"type": "text", "text": "s"}])])
    check("content parts joined", t == "Parts", str(t))
    t, e = run_llm([FakeResp(200, ValueError("bad json"))])
    check("bad json handled", t is None and "unexpected" in e)
    run_llm([completion("OK")], provider="custom")
    check("reasoning param only for OpenRouter", "reasoning" not in calls[0])
    run_llm([completion("OK")])
    check("reasoning low for OpenRouter", calls[0].get("reasoning") == {"effort": "low"})

    # --- PCAI provider defaults ------------------------------------------------------------------
    check("PCAI endpoint prefilled", appmod.DEFAULT_PCAI_BASE_URL.endswith(".imid.local") and appmod.DEFAULT_PCAI_MODEL == "qwen3.8-27b")
    fresh = make_q()
    await step("fresh state", appmod.serve(fresh))
    check("no token -> simulation by default", fresh.client.ai_provider == "simulation" and not fresh.client.llm_api_key)
    check("endpoint+model prefilled in settings state", fresh.client.llm_base_url == appmod.DEFAULT_PCAI_BASE_URL
          and fresh.client.llm_model == "qwen3.8-27b")
    import os as _os
    _os.environ["LLM_API_KEY"] = "dummy-test-token"
    try:
        f2 = make_q()
        await step("fresh state with LLM_API_KEY env", appmod.serve(f2))
        check("token env -> provider custom (PCAI)", f2.client.ai_provider == "custom")
        cfg = appmod.ai_config(f2)
        check("PCAI config uses /v1 base + model", cfg["base"].endswith("/v1") and cfg["model"] == "qwen3.8-27b", str(cfg))
        check("TLS verify on by default", cfg.get("verify", True) is True)
    finally:
        del _os.environ["LLM_API_KEY"]
    f3 = make_q()
    await step("fresh state (custom, no token)", appmod.serve(f3))
    f3.client.ai_provider = "custom"
    check("status asks for the token", "token needed" in appmod.provider_status(f3)[1])
    f3.client.llm_api_key = "t"
    f3.client.ai_last_error = "Access denied (401)"
    check("status shows rejected token", "rejected" in appmod.provider_status(f3)[1])

    # --- manufacturing features -----------------------------------------------------------------
    qq = make_q(make_q().client)
    await step("fresh app for manufacturing tests", appmod.serve(qq))
    m = qq.client
    for tab in ["production", "maintenance"]:
        q2 = make_q(m, **{"#": tab, "_submit": "#"})
        await step(f"tab #{tab}", appmod.serve(q2))
        check(f"active_tab=#{tab}", m.active_tab == "#" + tab)
    pp = appmod.plant_prod(qq)
    check("healthy plant OEE is realistic", 0.80 <= pp["oee"] <= 0.95, str(pp["oee"]))
    units0 = m.prod["cnc-01"]["units"]
    for _ in range(5):
        appmod.apply_tick(qq)
    check("production counters advance", m.prod["cnc-01"]["units"] > units0 and m.prod["cnc-01"]["kwh"] > 0)
    oee_before = m.prod["hyd-03"]["oee"]
    m.drift["hyd-03"] = "pressure"
    for _ in range(8):
        appmod.apply_tick(qq)
    check("a degrading machine loses OEE", m.prod["hyd-03"]["oee"] < oee_before - 0.1, f"{oee_before}->{m.prod['hyd-03']['oee']}")
    check("plant OEE drops with it", appmod.plant_prod(qq)["oee"] < pp["oee"])
    check("OEE history keeps its length", len(m.prod["hyd-03"]["hist"]) == appmod.HISTORY_LEN)
    m.drift = {}
    check("copilot snapshot has production + maintenance", "production" in appmod.fleet_snapshot(qq) and "maintenance" in appmod.fleet_snapshot(qq))
    check("rule answer: OEE", "OEE" in appmod.rule_based_answer(qq, "What is the plant OEE?"))
    check("rule answer: spare parts", "spare" in appmod.rule_based_answer(qq, "Which spare parts are low?").lower())
    m.running = False

    # parts + PM
    low = [p["id"] for p in m.parts if appmod.part_status(p) == "Reorder"]
    check("template has parts at reorder level", len(low) >= 2, str(low))
    pid = low[0]
    q2 = make_q(m, parts_tbl=[pid], _submit="parts_tbl")
    await step("order a part", appmod.serve(q2))
    check("part on order", appmod.find_part(q2, pid)["on_order"] and appmod.part_status(appmod.find_part(q2, pid)) == "On order")
    stock0 = appmod.find_part(q2, pid)["stock"]
    q2 = make_q(m, parts_tbl=[pid], _submit="parts_tbl")
    await step("receive the delivery", appmod.serve(q2))
    check("delivery adds the reorder quantity", appmod.find_part(q2, pid)["stock"] > stock0 and not appmod.find_part(q2, pid)["on_order"])
    over = [t for t in m.pm if appmod.pm_status(t) == "Overdue"]
    check("template has overdue PM", len(over) >= 1)
    q2 = make_q(m, pm_tbl=[over[0]["id"]], _submit="pm_tbl")
    await step("mark PM done", appmod.serve(q2))
    check("PM reset to its interval", over[0]["due_in_days"] == over[0]["interval_days"])

    # work order knowledge base + part consumption
    q2 = make_q(m, inject_machine="hyd-03", inject_sensor="pressure", inject_anomaly=True, _submit="inject_anomaly")
    await step("inject pressure fault", appmod.serve(q2))
    wo = next(a for a in m.actions if a["machine"] == "Hydraulic Press #3")
    check("WO knows its sensor key", wo["sensor_key"] == "pressure")
    m.dialog_wo = wo["id"]
    dlg = appmod.wo_dialog(q2)
    kinds = [type(i).__name__ for i in dlg.items]
    check("dialog has SOP/LOTO expanders", kinds.count("Component") >= 4 or any("expander" in str(i).lower() for i in dlg.items), str(kinds))
    check("dialog has the safety/LOTO expander", any(getattr(getattr(i, "expander", None), "label", "") .startswith("Safety") for i in dlg.items))
    q2 = make_q(m, wo_diag=wo["id"], _submit="wo_diag")
    await step("AI diagnosis (rule-based)", appmod.serve(q2))
    check("diagnosis stored", wo.get("diag") and "Most likely cause" in wo["diag"][0], str(wo.get("diag")))
    seal = appmod.find_part(q2, "seal-kit")["stock"]
    q2 = make_q(m, wo_assign=wo["id"], engineer="eng-01", _submit="wo_assign")
    await step("assign for completion", appmod.serve(q2))
    q2 = make_q(m, wo_complete=wo["id"], _submit="wo_complete")
    await step("complete WO", appmod.serve(q2))
    check("completion consumed the seal kit", appmod.find_part(q2, "seal-kit")["stock"] == seal - 1 and wo["parts_used"], str(wo.get("parts_used")))

    # handover
    check("handover mentions shift + OEE", "handover" in appmod.handover_text(q2) and "OEE" in appmod.handover_text(q2))
    q2 = make_q(m, handover_ai=True, _submit="handover_ai")
    await step("handover AI (simulation) falls back", appmod.serve(q2))
    check("fallback message stored", m.handover_ai and m.handover_ai[0] is None)
    m.ai_provider = "custom"
    m.llm_api_key = "t"
    m.ai_cooldown_until = 0
    LLM_CALLS.clear()
    q2 = make_q(m, handover_ai=True, _submit="handover_ai")
    await step("handover AI (PCAI mock)", appmod.serve(q2))
    check("handover AI text stored", m.handover_ai and m.handover_ai[0] and len(LLM_CALLS) == 1)
    wo2 = {"id": "WO-X", "machine": "Hydraulic Press #3", "issue": "Pressure drop", "reading": "1", "limit": "2",
           "severity": "Critical", "sensor_key": "pressure", "state": "Open"}
    LLM_CALLS.clear()
    txt, src = await appmod.diagnose_wo(q2, wo2)
    check("AI diagnosis uses the LLM when ready", len(LLM_CALLS) == 1 and "PCAI" in src, src)
    m.ai_provider = "simulation"

    # --- settings rendering for every provider, live loop, reset ------------------------
    for p in ("simulation", "openrouter", "custom"):
        qq = make_q(c, ai_provider=p, _submit="ai_provider")
        qq.client.active_tab = "#settings"
        await step(f"settings render ({p})", appmod.serve(qq))

    qq = make_q(c, toggle_live=True, _submit="toggle_live")
    await step("toggle live on", appmod.serve(qq))
    await asyncio.sleep(0.3)
    c.running = False
    await asyncio.sleep(0.1)

    qq = make_q(c, reset_sim=True, _submit="reset_sim")
    await step("reset", appmod.serve(qq))
    check("reset keeps AI provider", c.ai_provider == "custom")
    check("reset clears alerts", len(c.alerts) == 0 and c.chat == [])

    print("\n" + ("ALL PASSED" if not errors else f"{len(errors)} FAILURES: {errors}"))
    sys.exit(1 if errors else 0)

asyncio.run(main())
