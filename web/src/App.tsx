/* Copyright 2026 会议库 contributors
 * SPDX-License-Identifier: Apache-2.0 */
import { useEffect, useMemo, useRef, useState } from "react";
import { useVirtualizer } from "@tanstack/react-virtual";
import { api, readSse } from "./api";

type Runtime = {
  version: string;
  data_dir: string;
  asr_home?: string;
  ffmpeg: boolean;
  asr_exe: boolean;
  asr_python?: boolean;
  wizard_completed: boolean;
  worker_heartbeat_at?: string;
  sync_path_warning: boolean;
  share_lan?: boolean;
};

type LlmInfo = {
  id: number;
  task: string;
  model_name: string;
  endpoint: string;
  status: string;
  is_default: boolean;
};

function asrInstalled(runtime: Runtime): boolean {
  return Boolean(runtime.asr_exe || runtime.asr_python);
}

type Meeting = {
  id: number;
  title: string;
  status: string;
  occurred_at: string;
  duration_ms: number | null;
  group_id: number;
  has_audio: boolean;
  original_filename: string | null;
};
type Segment = {
  id: number;
  speaker_id: number | null;
  start_ms: number;
  end_ms: number;
  text: string;
  review_status: string;
};
type Speaker = {
  id: number;
  speaker_label: string;
  display_name: string;
  person_id: number | null;
  status: string;
};
type Job = {
  id: number;
  meeting_id: number | null;
  type: string;
  status: string;
  progress: { stage?: string; percent?: number; i?: number; n?: number } | null;
  error: { message?: string } | null;
};

const STEPS = ["预处理", "转写", "校对", "索引", "报告"];

function route(): string {
  return location.hash.replace(/^#/, "") || "/";
}

const STAGE_LABEL: Record<string, string> = {
  asr: "转写",
  ffmpeg: "预处理",
  load_models: "加载模型",
  generate: "识别中",
  finalize: "整理结果",
  embed: "建立索引",
  stats: "统计",
  report: "生成报告",
  digest: "生成摘要",
  preprocess: "预处理",
  transcribe: "转写",
  qa: "问答",
  "report-llm": "生成报告",
  "report-map": "分批摘要",
  "report-map-llm": "分批摘要",
  "report-reduce-llm": "汇总报告",
  "digest-map": "汇总各会议",
  "digest-map-llm": "汇总各会议",
  "digest-llm": "生成摘要",
};

function progressNote(p: { stage?: string; percent?: number; i?: number; n?: number } | undefined | null, type?: string): string {
  if (p?.stage) {
    const label = STAGE_LABEL[p.stage] || p.stage;
    if (p.percent != null) return `${label} ${p.percent}%`;
    if (p.i != null) return `${label} ${p.i}${p.n != null ? `/${p.n}` : ""}`;
    return label;
  }
  return (type && STAGE_LABEL[type]) || type || "";
}

async function waitJob(jobId: number, onNote: (s: string) => void): Promise<Job> {
  const started = Date.now();
  for (;;) {
    const job = await api<Job>(`/api/jobs/${jobId}`);
    if (Date.now() - started > 3000) {
      const p = job.progress;
      onNote(progressNote(p, job.type) || "仍在处理，请稍候…");
    }
    if (job.status === "success") return job;
    if (job.status === "failed" || job.status === "cancelled") {
      throw new Error(job.error?.message || job.status);
    }
    if (Date.now() - started > 15000 && job.status === "queued") {
      onNote("已排队，等待后台任务");
    }
    if (Date.now() - started > 30 * 60 * 1000) {
      throw new Error("后台任务长时间无响应，请检查服务或刷新重试");
    }
    await new Promise((r) => setTimeout(r, 800));
  }
}

export function App() {
  const [path, setPath] = useState(route);
  const [runtime, setRuntime] = useState<Runtime | null>(null);
  const [err, setErr] = useState("");

  useEffect(() => {
    const on = () => setPath(route());
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);

  useEffect(() => {
    api<Runtime>("/api/runtime")
      .then(setRuntime)
      .catch((e) => {
        if (String(e.message).includes("unauthorized")) location.hash = "/login";
        else setErr(String(e.message));
      });
  }, [path]);

  useEffect(() => {
    if (runtime && !runtime.wizard_completed && path !== "/setup") {
      location.hash = "/setup";
    }
  }, [runtime, path]);

  useEffect(() => {
    const onShare = (ev: Event) => {
      const on = Boolean((ev as CustomEvent<boolean>).detail);
      setRuntime((prev) => (prev ? { ...prev, share_lan: on } : prev));
    };
    window.addEventListener("huiyiku-share", onShare);
    return () => window.removeEventListener("huiyiku-share", onShare);
  }, []);

  if (err) return <div className="main err">{err}</div>;
  if (!runtime) {
    // 开启访问令牌后 runtime 会 401：登录页不能依赖 runtime，否则永远「加载中」
    if (path === "/login") {
      return (
        <div className="main" style={{ maxWidth: 460, paddingTop: 48 }}>
          <Login
            onDone={() => {
              api<Runtime>("/api/runtime").then(setRuntime).catch(() => undefined);
              location.hash = "/";
            }}
          />
        </div>
      );
    }
    return <div className="main muted">加载中…</div>;
  }

  return (
    <div className="app">
      <nav className="nav">
        <h1 className="brand">会议库</h1>
        <a href="#/" className={path === "/" ? "active" : ""}><IcoChat />会议</a>
        <a href="#/import" className={path === "/import" ? "active" : ""}><IcoImport />导入</a>
        <a href="#/actions" className={path.startsWith("/actions") ? "active" : ""}><IcoCheck />行动项</a>
        <a href="#/digest" className={path === "/digest" ? "active" : ""}><IcoTrend />项目进展</a>
        <a href="#/settings" className={path === "/settings" ? "active" : ""}><IcoGear />设置</a>
        <button type="button" className="nav-refresh" title="刷新页面（加载最新界面）" onClick={() => location.reload()}>
          <IcoReload />刷新
        </button>
        <div className="foot">
          v{runtime.version}
          <div>
            <span className={"dot" + (runtime.ffmpeg ? "" : " bad")}>●</span> FFmpeg{" "}
            {runtime.ffmpeg ? "已找到" : "未找到"}
          </div>
          <div>
            <span className={"dot" + (asrInstalled(runtime) ? "" : " bad")}>●</span> ASR{" "}
            {asrInstalled(runtime) ? "组件已装" : "需安装组件"}
          </div>
          <div>
            <span className={"dot" + (runtime.share_lan ? " warn" : " off")}>●</span>
            <a href="#/settings">内网共享 {runtime.share_lan ? "已开启" : "已关闭"}</a>
          </div>
        </div>
      </nav>
      <div className="main">
        {runtime.sync_path_warning && (
          <div className="card warn">数据目录像在同步盘/网络盘上，WAL 可能不可靠。请改到本地磁盘。</div>
        )}
        {path === "/setup" && (
          <Setup
            runtime={runtime}
            onDone={async () => {
              const next = await api<Runtime>("/api/runtime");
              setRuntime(next);
              location.hash = "/";
            }}
          />
        )}
        {path === "/login" && <Login onDone={() => (location.hash = "/")} />}
        {path === "/" && <Library />}
        {path === "/import" && <ImportPage />}
        {path === "/actions" && <ActionsPage />}
        {path === "/digest" && <DigestPage />}
        {path === "/settings" && <SettingsPage />}
        {path.startsWith("/meetings/") && (
          // key=会议 id：切换会议时重新挂载，问答回答/补充/引用等状态
          // 不再残留到下一场会议
          <MeetingPage key={Number(path.split("/")[2]) || 0} id={Number(path.split("/")[2]) || 0} />
        )}
      </div>
    </div>
  );
}

function Setup({ runtime, onDone }: { runtime: Runtime; onDone: () => void | Promise<void> }) {
  const [legal, setLegal] = useState(false);
  const [egress, setEgress] = useState(false);
  const [llmEndpoint, setLlmEndpoint] = useState("https://api.deepseek.com");
  const [llmModel, setLlmModel] = useState("deepseek-chat");
  const [llmKey, setLlmKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");
  return (
    <div className="card" style={{ maxWidth: 560 }}>
      <h2>首次设置</h2>
      <p>转写在本机完成，音频默认不上传。报告/问答会把转写文本发到你自己的 LLM。</p>
      <p className="muted">
        本工具不是窃听软件。美国多个州未经全体参会人同意录音可构成犯罪；欧盟工作场景录同事/客户通常不受
        GDPR 家庭豁免；中国向第三方提供个人信息应告知并取得同意。详见文档「法律与许可」。
      </p>
      <p className="muted">
        FFmpeg {runtime.ffmpeg ? "已找到" : "未找到，导入前请安装到 PATH 或填写 ffmpeg_path"} · ASR{" "}
        {asrInstalled(runtime)
          ? "组件已装"
          : `未安装，请解压到 ${runtime.asr_home || "程序目录/asr"}`}
      </p>
      <label className="check">
        <input type="checkbox" checked={legal} onChange={(e) => setLegal(e.target.checked)} />
        <span>
          我理解：录音须符合当地法律，并已获得（或将获得）必要的参会人同意。本工具不做同意管理。此勾选不能代替守法。
        </span>
      </label>
      <label className="check">
        <input type="checkbox" checked={egress} onChange={(e) => setEgress(e.target.checked)} />
        <span>我同意把文本发到自己配置的 API（可稍后）</span>
      </label>
      <h3>远程 LLM（可选）</h3>
      <p className="cloud">仅报告/问答/摘要需要。不填 Key 仍可转写。</p>
      <p className="muted">
        启用需填两项：<strong>Base URL</strong> 和 <strong>API Key</strong>（另有「模型名称」）。Base URL
        填到域名或 <code>/v1</code> 均可，<code>/v1/chat/completions</code> 会自动补全。启用前请自行确认该厂商的条款、数据保留与跨境传输；本项目不内置、不代理账单。
      </p>
      <p className="muted">
        API 格式：<strong>OpenAI Chat Completions（/v1/chat/completions）</strong>。仅支持兼容该格式的服务，如
        DeepSeek、通义、硅基流动、OpenAI 等；暂不支持 Anthropic Messages（/v1/messages）或 Responses（/v1/responses）格式，填这两类端点会错配。
      </p>
      <label className="muted" htmlFor="llm-endpoint">
        Base URL（API 端点）
      </label>
      <input id="llm-endpoint" placeholder="https://api.deepseek.com 或 https://api.deepseek.com/v1" value={llmEndpoint} onChange={(e) => setLlmEndpoint(e.target.value)} />
      <p />
      <label className="muted" htmlFor="llm-model">
        模型名称
      </label>
      <input id="llm-model" placeholder="deepseek-chat" value={llmModel} onChange={(e) => setLlmModel(e.target.value)} />
      <p />
      <label className="muted" htmlFor="llm-key">
        API Key（仅存本机 secrets.json，不上传、不入库）
      </label>
      <input id="llm-key" placeholder="sk-..." type="password" value={llmKey} onChange={(e) => setLlmKey(e.target.value)} />
      {llmKey && !egress && <p className="cloud">填写 Key 前请勾选上方「同意把文本发到自己配置的 API」。</p>}
      <p />
      <button
        type="button"
        className="primary"
        disabled={!legal || busy}
        onClick={async () => {
          setBusy(true);
          try {
            await api("/api/setup", {
              method: "POST",
              body: JSON.stringify({
                legal_confirmed: legal,
                egress_confirmed: egress,
                llm_endpoint: llmKey ? llmEndpoint : "",
                llm_model: llmKey ? llmModel : "",
                llm_api_key: llmKey,
              }),
            });
            onDone();
          } catch (e) {
            setMsg(String(e));
          } finally {
            setBusy(false);
          }
        }}
      >
        {busy ? "提交中…" : "进入会议库"}
      </button>
      {msg && <p className="err">{msg}</p>}
    </div>
  );
}

function Login({ onDone }: { onDone: () => void }) {
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");
  const submit = async () => {
    if (busy || !token) return;
    setBusy(true);
    try {
      await api("/api/session", { method: "POST", body: JSON.stringify({ token }) });
      onDone();
    } catch (e) {
      setMsg(String(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="card" style={{ maxWidth: 420 }}>
      <h2>访问令牌</h2>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          void submit();
        }}
      >
        <input
          type="password"
          aria-label="访问令牌"
          value={token}
          onChange={(e) => setToken(e.target.value)}
        />
        <p />
        <button type="submit" className="primary" disabled={busy || !token}>
          {busy ? "验证中…" : "进入"}
        </button>
      </form>
      {msg && <p className="err">{msg}</p>}
    </div>
  );
}

function Library() {
  const [items, setItems] = useState<Meeting[]>([]);
  const [groups, setGroups] = useState<{ id: number; name: string }[]>([]);
  const [groupId, setGroupId] = useState("");
  useEffect(() => {
    api<typeof groups>("/api/groups").then(setGroups);
  }, []);
  useEffect(() => {
    const q = groupId ? `?group_id=${groupId}` : "";
    api<Meeting[]>(`/api/meetings${q}`).then(setItems);
  }, [groupId]);
  return (
    <div>
      <div className="row">
        <h2 style={{ margin: 0 }}>会议</h2>
        <select value={groupId} onChange={(e) => setGroupId(e.target.value)} style={{ width: 180 }}>
          <option value="">全部组</option>
          {groups.map((g) => (
            <option key={g.id} value={g.id}>{g.name}</option>
          ))}
        </select>
        <a href="#/import"><button type="button" className="primary">导入</button></a>
      </div>
      {items.length === 0 && <div className="empty">还没有会议。从「导入」添加录音。</div>}
      {items.map((m) => (
        <a key={m.id} href={`#/meetings/${m.id}`} style={{ textDecoration: "none", color: "inherit" }}>
          <div className="meeting-card">
            <div className="row" style={{ justifyContent: "space-between" }}>
              <strong>{m.title}</strong>
              <StatusBadge status={m.status} />
            </div>
            <div className="muted">
              <span className="t">{fmtDate(m.occurred_at)}</span>
              {m.original_filename ? ` · ${m.original_filename}` : ""}
            </div>
          </div>
        </a>
      ))}
    </div>
  );
}

function ImportPage() {
  const [msg, setMsg] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const [over, setOver] = useState(false);

  const upload = async (file: File) => {
    setBusy(true);
    setErr("");
    setMsg("创建会议…");
    let created: Meeting | null = null;
    try {
      created = await api<Meeting>("/api/meetings", {
        method: "POST",
        body: JSON.stringify({ title: file.name.replace(/\.[^.]+$/, "") }),
      });
      setMsg("上传中…");
      const res = await fetch(`/api/meetings/${created.id}/media?filename=${encodeURIComponent(file.name)}`, {
        method: "POST",
        body: file,
        // header 值只允许 ISO-8859-1，中文文件名必须编码，否则 fetch 直接抛 TypeError
        headers: { "x-filename": encodeURIComponent(file.name) },
      });
      if (!res.ok) throw new Error(await res.text());
      location.hash = `/meetings/${created.id}`;
    } catch (err) {
      setMsg("");
      setErr(String(err));
      // 上传失败回滚刚创建的空会议，避免列表里留下无媒体的孤儿条目
      if (created) {
        await api(`/api/meetings/${created.id}?confirm=true`, { method: "DELETE" }).catch(() => undefined);
      }
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="card" style={{ maxWidth: 560 }}>
      <h2>导入</h2>
      <p>支持 WAV / MP3 / M4A / MP4。大文件请用状态窗口「添加本地文件」。</p>
      <p className="muted">
        请只导入你有权处理的录音。许多地方未经全体参会人同意录音可能违法；工作场合通常需要告知。
      </p>
      <div
        className={"drop" + (over ? " over" : "")}
        onDragOver={(e) => {
          e.preventDefault();
          setOver(true);
        }}
        onDragLeave={() => setOver(false)}
        onDrop={(e) => {
          e.preventDefault();
          setOver(false);
          const file = e.dataTransfer.files[0];
          if (file && !busy) void upload(file);
        }}
      >
        <p>把录音拖到这里，或选择文件</p>
        <input
          type="file"
          accept=".wav,.mp3,.m4a,.mp4"
          disabled={busy}
          onChange={(e) => {
            const file = e.target.files?.[0];
            if (file) void upload(file);
          }}
        />
      </div>
      {busy && <p className="muted">{msg}</p>}
      {err && <p className="err">{err}</p>}
    </div>
  );
}

const QA_EXTRA_MARKER = "【AI 补充（非录音内容）】";

type QaTurn = { q: string; a: string; x: string; cites: any[] };

function MeetingPage({ id }: { id: number }) {
  const [m, setM] = useState<Meeting | null>(null);
  const [segs, setSegs] = useState<Segment[]>([]);
  const [speakers, setSpeakers] = useState<Speaker[]>([]);
  const [stats, setStats] = useState<any>(null);
  const [report, setReport] = useState<any>(null);
  const [repEditing, setRepEditing] = useState(false);
  const [repDraft, setRepDraft] = useState<any>(null);
  const [tab, setTab] = useState<"review" | "report" | "qa" | "people">("review");
  const [picked, setPicked] = useState<number[]>([]);
  const [editingSeg, setEditingSeg] = useState<number | null>(null);
  const [runtime, setRuntime] = useState<Runtime | null>(null);
  const [editDraft, setEditDraft] = useState("");
  const editCancelRef = useRef(false);
  const [activeJob, setActiveJob] = useState(false);
  const [follow, setFollow] = useState(true);
  const audioRef = useRef<HTMLAudioElement>(null);
  const [active, setActive] = useState<number | null>(null);
  const [playMs, setPlayMs] = useState(0);
  const [qaQ, setQaQ] = useState("");
  const [qaExtra, setQaExtra] = useState(false);
  const qaInputRef = useRef<HTMLTextAreaElement | null>(null);
  const [threadId, setThreadId] = useState("");
  const [qaTurns, setQaTurns] = useState<QaTurn[]>([]);
  const [threadScope, setThreadScope] = useState("");
  const [qaScope, setQaScope] = useState<"meeting" | "group">(() => {
    try {
      return sessionStorage.getItem("huiyiku-qa-scope") === "group" ? "group" : "meeting";
    } catch {
      return "meeting";
    }
  });
  const [qaType, setQaType] = useState("");
  const [qaFrom, setQaFrom] = useState("");
  const [qaTo, setQaTo] = useState("");
  const [qaPerson, setQaPerson] = useState(0);
  const [persons, setPersons] = useState<any[]>([]);
  const [busy, setBusy] = useState("");
  const [note, setNote] = useState("");
  const [flash, setFlash] = useState("");
  const listRef = useRef<HTMLDivElement>(null);

  const onErr = (e: unknown) => {
    if (String(e).includes("unauthorized")) location.hash = "/login";
  };
  const reload = () => {
    api<Meeting>(`/api/meetings/${id}`).then(setM).catch(onErr);
    api<Segment[]>(`/api/meetings/${id}/transcript?limit=2000`).then(setSegs).catch(onErr);
    api<Speaker[]>(`/api/meetings/${id}/speakers`).then(setSpeakers).catch(onErr);
    api(`/api/meetings/${id}/stats`).then(setStats).catch(() => setStats(null));
    api(`/api/meetings/${id}/report`).then(setReport).catch(() => setReport(null));
  };
  useEffect(() => {
    reload();
    const t = setInterval(() => {
      api<Meeting>(`/api/meetings/${id}`).then(setM).catch(onErr);
    }, 4000);
    return () => clearInterval(t);
  }, [id]);
  useEffect(() => {
    api<any[]>("/api/persons").then(setPersons).catch(() => setPersons([]));
    api<Runtime>("/api/runtime").then(setRuntime).catch(() => undefined);
  }, []);
  // 问答线程：按会议 id 持久化（刷新/重进接上），历史从服务端拉取
  useEffect(() => {
    let tid = "";
    try {
      tid = localStorage.getItem(`huiyiku-qa-thread-${id}`) || "";
    } catch {
      tid = "";
    }
    if (!tid) {
      tid = crypto.randomUUID();
      try {
        localStorage.setItem(`huiyiku-qa-thread-${id}`, tid);
      } catch {
        /* ignore */
      }
    }
    setThreadId(tid);
  }, [id]);
  useEffect(() => {
    if (!threadId) return;
    api<
      { id: number; question: string; answer: string; citations: any[]; allow_extra: boolean; created_at: string }[]
    >(`/api/qa/threads/${threadId}/messages`)
      .then((rows) =>
        setQaTurns(
          rows.map((r) => {
            const parts = String(r.answer || "").split(QA_EXTRA_MARKER);
            return {
              q: r.question,
              a: (parts[0] || "").trim(),
              x: (parts[1] || "").trim(),
              cites: r.citations || [],
            };
          }),
        ),
      )
      .catch(() => setQaTurns([]));
  }, [threadId]);
  const startThread = () => {
    const tid = crypto.randomUUID();
    try {
      localStorage.setItem(`huiyiku-qa-thread-${id}`, tid);
    } catch {
      /* ignore */
    }
    setThreadId(tid);
    setQaTurns([]);
    setThreadScope("");
  };
  useEffect(() => {
    if (!m) return;
    api<Segment[]>(`/api/meetings/${id}/transcript?limit=2000`).then(setSegs);
    api<Speaker[]>(`/api/meetings/${id}/speakers`).then(setSpeakers);
  }, [id, m?.status]);

  useEffect(() => {
    if (!m || !["preprocessing", "transcribing", "indexing", "report_draft"].includes(m.status)) {
      setActiveJob(false);  // 稳定态不再轮询，必须显式重置否则 retranscribe 永久禁用
      return;
    }
    const tick = async () => {
      try {
        const jobs = await api<Job[]>("/api/jobs");
        setActiveJob(jobs.some((j) => j.meeting_id === id && (j.status === "queued" || j.status === "running")));
        const mine = jobs.find((j) => j.meeting_id === id && (j.status === "queued" || j.status === "running"));
        if (!mine) {
          setNote("");
          setActiveJob(false);
          return;
        }
        setNote(progressNote(mine.progress, mine.type));
      } catch {
        /* ignore */
      }
    };
    void tick();
    const t = setInterval(() => void tick(), 2000);
    return () => clearInterval(t);
  }, [id, m?.status]);

  const spMap = useMemo(() => Object.fromEntries(speakers.map((s) => [s.id, s])), [speakers]);
  const workerStale =
    !!runtime?.worker_heartbeat_at &&
    Date.now() - new Date(runtime.worker_heartbeat_at).getTime() > 3 * 60 * 1000;
  const stuck =
    !!m &&
    ["preprocessing", "transcribing", "indexing", "report_draft"].includes(m.status) &&
    workerStale;
  const step = stepIndex(m?.status || "");
  const reviewed = segs.filter((s) => s.review_status === "edited").length;

  const virtualizer = useVirtualizer({
    count: segs.length,
    getScrollElement: () => listRef.current,
    estimateSize: () => 88,
    overscan: 10,
    getItemKey: (index) => segs[index]?.id ?? index,
  });

  const seek = (ms: number, segId?: number) => {
    const el = audioRef.current;
    if (el) el.currentTime = ms / 1000;
    setPlayMs(ms);
    if (segId != null) {
      setActive(segId);
      const idx = segs.findIndex((s) => s.id === segId);
      if (idx >= 0) virtualizer.scrollToIndex(idx, { align: "center" });
    }
  };

  // 报告条目「就这条追问」：切到问答页签并预填到当前线程，范围锁本场 + 转写原文
  const askAbout = (text: string) => {
    setTab("qa");
    setQaQ(`关于「${text}」：`);
    setQaType("transcript");
    setThreadScope(""); // 主题切换：下轮提交按新 scope 重建快照，线程延续
    setTimeout(() => qaInputRef.current?.focus(), 50);
  };

  const run = async (key: string, fn: () => Promise<void>) => {
    setBusy(key);
    setFlash("");
    setNote("");
    try {
      await fn();
    } catch (e) {
      setFlash(String(e));
    } finally {
      setBusy("");
      setNote("");
    }
  };

  return (
    <div>
      <div className="row">
        <h2 style={{ margin: 0 }}>{m?.title || "会议"}</h2>
        {m?.status && <StatusBadge status={m.status} />}
        <span className="muted t">{fmtDate(m?.occurred_at || "")}</span>
        <a className="muted" href={`/api/export/meeting/${id}.md`} download title="分享前请确认已获参会人同意">
          导出 MD
        </a>
        <a className="muted" href={`/api/export/meeting/${id}.html`} download title="浏览器打开后打印可存 PDF；分享前请确认已获参会人同意">
          导出 HTML
        </a>
        <a className="muted" href={`/api/export/meeting/${id}.docx`} download title="分享前请确认已获参会人同意">
          导出 Word
        </a>
        <button
          type="button"
          className="danger"
          disabled={busy === "delete"}
          onClick={() =>
            run("delete", async () => {
              const preview = await api<any>(`/api/meetings/${id}`, { method: "DELETE" });
              const jobIds: number[] = (preview.impact?.jobs || []).map((j: any) => j.id);
              if (
                !confirm(
                  `删除「${preview.impact?.title || m?.title}」及其转写、报告和媒体？${
                    jobIds.length ? `将先取消 ${jobIds.length} 个进行中任务。` : ""
                  }`,
                )
              ) {
                return;
              }
              // 后端要求先取消进行中任务；否则 confirm 删除返回 409
              for (const jid of jobIds) {
                await api(`/api/jobs/${jid}/cancel`, { method: "POST" }).catch(() => undefined);
              }
              for (let i = 0; i < 30; i++) {
                await new Promise((r) => setTimeout(r, 1000));
                // 按 id 逐个查（列表端点只返回最近 50 条，分页会漏）
                const states = await Promise.all(
                  jobIds.map((jid) =>
                    api<any>(`/api/jobs/${jid}`)
                      .then((j) => j.status)
                      .catch(() => "failed"),
                  ),
                );
                if (states.every((s) => ["cancelled", "failed", "success"].includes(s))) break;
              }
              try {
                await api(`/api/meetings/${id}?confirm=true`, { method: "DELETE" });
                location.hash = "/";
              } catch (e) {
                if (String(e).includes("cancel in-progress")) {
                  throw new Error("后台任务仍在取消中，请稍后再删除");
                }
                throw e;
              }
            })
          }
        >
          {busy === "delete" ? "删除中…" : "删除"}
        </button>
      </div>
      <div className="steps">
        {STEPS.map((s, i) => (
          <span key={s} className={i <= step ? "on" : ""}>{s}</span>
        ))}
      </div>
      {m && ["preprocessing", "transcribing", "indexing", "report_draft"].includes(m.status) && (
        <div className="prog-row">
          <div className="prog-track">
            {(() => {
              const m2 = note.match(/(\d+)%/);
              const pct = m2 ? Math.min(100, Number(m2[1])) : null;
              return pct != null ? (
                <div className="prog-fill" style={{ width: `${pct}%` }} />
              ) : (
                <div className="prog-fill indeterminate" />
              );
            })()}
          </div>
          {note && <span className="muted prog-note">{note}</span>}
        </div>
      )}
      {flash && <p className="err flash">{flash}</p>}
      <div className="row">
        <button type="button" className={tab === "review" ? "on" : ""} onClick={() => setTab("review")}>校对</button>
        <button type="button" className={tab === "report" ? "on" : ""} onClick={() => setTab("report")}>报告</button>
        <button type="button" className={tab === "qa" ? "on" : ""} onClick={() => setTab("qa")}>问答</button>
        <button type="button" className={tab === "people" ? "on" : ""} onClick={() => setTab("people")}>参会人</button>
        {m?.status === "speaker_review" && (
          <button
            type="button"
            className="primary"
            disabled={busy === "approve"}
            onClick={() =>
              run("approve", async () => {
                const res = await api<{ job_id: number }>(`/api/meetings/${id}/speakers/approve`, { method: "POST" });
                if (res.job_id) await waitJob(res.job_id, setNote);
                reload();
              })
            }
          >
            {busy === "approve" ? "提交中…" : "接受自动标记并索引"}
          </button>
        )}
        {stuck && (
          <button
            type="button"
            disabled={busy === "retry"}
            onClick={() =>
              run("retry", async () => {
                const res = await api<{ job_id: number }>(`/api/meetings/${id}/retry?stage=auto`, {
                  method: "POST",
                });
                if (res.job_id) await waitJob(res.job_id, setNote);
                reload();
              })
            }
          >
            {busy === "retry" ? "重试中…" : "后台无响应，点击恢复"}
          </button>
        )}
        {m?.status === "failed" && (
          <button
            type="button"
            disabled={busy === "retry"}
            onClick={() =>
              run("retry", async () => {
                const res = await api<{ job_id: number }>(`/api/meetings/${id}/retry?stage=auto`, {
                  method: "POST",
                });
                if (res.job_id) await waitJob(res.job_id, setNote);
                reload();
              })
            }
          >
            {busy === "retry" ? "重试中…" : "重试失败步骤"}
          </button>
        )}
        {m && ["speaker_review", "indexed", "report_draft", "ready"].includes(m.status) && m.has_audio && (
          <button
            type="button"
            disabled={busy === "retranscribe" || activeJob}
            onClick={() => {
              if (!confirm("重新转写会覆盖现有转写与校对结果，确定？")) return;
              void run("retranscribe", async () => {
                const res = await api<{ job_id: number }>(
                  `/api/meetings/${id}/retry?stage=transcribe`,
                  { method: "POST" },
                );
                if (res.job_id) await waitJob(res.job_id, setNote);
                reload();
              });
            }}
          >
            {busy === "retranscribe" ? "转写中…" : "重新转写"}
          </button>
        )}
      </div>
      <div className="layout-2">
        <div className="transcript">
          {segs.length === 0 && <div className="empty">暂无转写。导入后等待预处理与转写。</div>}
          {segs.length > 0 && (
            <>
              <div className="transcript-toolbar muted">
                <span>校对进度 {reviewed}/{segs.length} 段</span>
                <label className="check" style={{ margin: 0 }}>
                  <input type="checkbox" checked={follow} onChange={(e) => setFollow(e.target.checked)} />
                  <span>跟随播放</span>
                </label>
              </div>
              <div className="transcript-list" ref={listRef}>
              <div style={{ height: virtualizer.getTotalSize(), position: "relative" }}>
                {virtualizer.getVirtualItems().map((vi) => {
                  const s = segs[vi.index];
                  const dur = Math.max(1, s.end_ms - s.start_ms);
                  const pct =
                    active === s.id ? Math.min(100, Math.max(0, ((playMs - s.start_ms) / dur) * 100)) : 0;
                  return (
                    <div
                      key={s.id}
                      data-index={vi.index}
                      ref={virtualizer.measureElement}
                      className={"seg" + (active === s.id ? " active" : "")}
                      role="button"
                      tabIndex={0}
                      aria-label={`转写 ${fmt(s.start_ms)}`}
                      style={{
                        position: "absolute",
                        top: 0,
                        left: 0,
                        width: "100%",
                        transform: `translateY(${vi.start}px)`,
                      }}
                      onClick={() => seek(s.start_ms, s.id)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter" || e.key === " ") {
                          e.preventDefault();
                          seek(s.start_ms, s.id);
                        }
                      }}
                    >
                      {pct > 0 && <div className="seg-progress" style={{ width: `${pct}%` }} />}
                      <div className="meta">
                        <input
                          type="checkbox"
                          checked={picked.includes(s.id)}
                          onClick={(e) => e.stopPropagation()}
                          onChange={(e) => {
                            setPicked((p) => (e.target.checked ? [...p, s.id] : p.filter((x) => x !== s.id)));
                          }}
                        />{" "}
                        <span className="t">{fmt(s.start_ms)}</span> · {spMap[s.speaker_id || -1]?.display_name || "?"} · {s.review_status === "edited" ? "已编辑" : s.review_status === "confirmed" ? "已确认" : "自动"}
                      </div>
                      {editingSeg === s.id ? (
                        <textarea
                          autoFocus
                          className="seg-edit"
                          value={editDraft}
                          onClick={(e) => e.stopPropagation()}
                          onChange={(e) => setEditDraft(e.target.value)}
                          onBlur={async () => {
                            if (editCancelRef.current) {
                              editCancelRef.current = false;
                              setEditingSeg(null);
                              return;
                            }
                            const text = editDraft.trim();
                            setEditingSeg(null);
                            if (text !== s.text) {
                              await api(`/api/transcript-segments/${s.id}`, {
                                method: "PATCH",
                                body: JSON.stringify({ text }),
                              })
                                .then(reload)
                                .catch((e2) => setFlash(`保存失败：${String(e2)}`));
                            }
                          }}
                          onKeyDown={(e) => {
                            e.stopPropagation();
                            if (e.key === "Escape") {
                              editCancelRef.current = true;
                              setEditingSeg(null);
                            }
                          }}
                          rows={2}
                        />
                      ) : (
                        <div
                          className="body"
                          title="双击修改文字"
                          onDoubleClick={(e) => {
                            e.stopPropagation();
                            editCancelRef.current = false;  // 上次 Esc 残留旗标不吞本次合法保存
                            setEditingSeg(s.id);
                            setEditDraft(s.text);
                          }}
                        >
                          {s.text}
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
              </div>
            </>
          )}
        </div>
        <div>
          <audio
            ref={audioRef}
            controls
            src={`/api/meetings/${id}/audio`}
            style={{ width: "100%" }}
            onTimeUpdate={(e) => {
              const t = e.currentTarget.currentTime * 1000;
              setPlayMs(t);
              const hit = segs.find((s) => t >= s.start_ms && t < s.end_ms);
              if (hit && hit.id !== active) {
                setActive(hit.id);
                if (follow) {
                  const idx = segs.findIndex((s) => s.id === hit.id);
                  if (idx >= 0) virtualizer.scrollToIndex(idx, { align: "auto" });
                }
              }
            }}
          />
          <p className="muted">播放走会话 Cookie，不会把整段 WAV 读进 JS。</p>
          {tab === "review" && (
            <div className="card">
              <h3>说话人</h3>
              {speakers.length === 0 && <p className="muted">转写完成后在这里核对说话人。</p>}
              {speakers.map((s) => (
                <div key={s.id} className="row" style={{ marginBottom: 8 }}>
                  <input
                    defaultValue={s.display_name}
                    onBlur={(e) =>
                      api(`/api/meeting-speakers/${s.id}`, {
                        method: "PATCH",
                        body: JSON.stringify({ display_name: e.target.value }),
                      }).then(reload)
                    }
                  />
                  <button
                    type="button"
                    disabled={!picked.length || busy === "assign"}
                    onClick={() =>
                      run("assign", async () => {
                        await api(`/api/meeting-speakers/${s.id}/assign`, {
                          method: "POST",
                          body: JSON.stringify({ segment_ids: picked }),
                        });
                        setPicked([]);
                        reload();
                      })
                    }
                  >
                    {busy === "assign" ? "划入中…" : "划入所选"}
                  </button>
                  <select
                    defaultValue=""
                    style={{ width: 120 }}
                    disabled={busy === `merge-${s.id}`}
                    onChange={(e) => {
                      const into = Number(e.target.value);
                      const intoName = speakers.find((o) => o.id === into)?.display_name || "";
                      e.target.value = "";
                      if (!into) return;
                      if (!confirm(`将「${s.display_name}」合并到「${intoName}」？`)) return;
                      void run(`merge-${s.id}`, async () => {
                        await api(`/api/meeting-speakers/${s.id}/merge`, {
                          method: "POST",
                          body: JSON.stringify({ into_id: into }),
                        });
                        reload();
                      });
                    }}
                  >
                    <option value="">合并到…</option>
                    {speakers.filter((o) => o.id !== s.id).map((o) => (
                      <option key={o.id} value={o.id}>{o.display_name}</option>
                    ))}
                  </select>
                  {s.status !== "invalid" && s.status !== "merged" && (
                    <button
                      type="button"
                      className="danger"
                      disabled={busy === `invalid-${s.id}`}
                      onClick={() => {
                        if (!confirm(`将「${s.display_name}」标为无效？其发言不再计入统计。`)) return;
                        void run(`invalid-${s.id}`, async () => {
                          await api(`/api/meeting-speakers/${s.id}`, {
                            method: "PATCH",
                            body: JSON.stringify({ status: "invalid" }),
                          });
                          reload();
                        });
                      }}
                    >
                      {busy === `invalid-${s.id}` ? "…" : "无效"}
                    </button>
                  )}
                </div>
              ))}
              <button
                type="button"
                disabled={busy === "speaker"}
                onClick={() =>
                  run("speaker", async () => {
                    await api(`/api/meetings/${id}/speakers`, { method: "POST" });
                    reload();
                  })
                }
              >
                {busy === "speaker" ? "创建中…" : "新建说话人"}
              </button>
              {stats?.speakers && (
                <div>
                  <h3>单场统计 {stats.metric_version}</h3>
                  {stats.speakers.map((s: any) => (
                    <div key={s.speaker_id} className="muted">
                      {s.display_name}: {s.segment_count} 次 · {Math.round(s.speech_ratio * 100)}%
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}
          {tab === "report" && (
            <div className="card">
              <p className="cloud">生成报告会把转写文本发到你的 LLM。分享报告前请确认已获参会人同意。</p>
              {!report?.current && (
                <p className="muted">生成草稿后可核对每条内容的出处（时间点 · 人物）；接受后才会抽出行动项，并作为问答的高置信引用。</p>
              )}
              <button
                type="button"
                className="primary"
                disabled={busy === "report"}
                onClick={() =>
                  run("report", async () => {
                    const res = await api<{ job_id: number }>(`/api/meetings/${id}/report`, { method: "POST" });
                    if (res.job_id) await waitJob(res.job_id, setNote);
                    reload();
                  })
                }
              >
                {busy === "report" ? "生成中…" : "生成草稿"}
              </button>
              {report?.current && (
                <div>
                  <p>状态 {report.current.status} {report.current.stale_reason || ""}</p>
                  {repEditing ? (
                    <>
                      {report.current.status === "accepted" && (
                        <p className="muted">编辑会产生新草稿；原接受版本的行动项不变，接受新版本后更新。</p>
                      )}
                      <ReportEditor draft={repDraft} persons={persons} onChange={setRepDraft} />
                      <button
                        type="button"
                        className="primary"
                        disabled={busy === "save_report"}
                        onClick={() =>
                          run("save_report", async () => {
                            const c = repDraft || {};
                            const clone = (v: any) => JSON.parse(JSON.stringify(v ?? []));
                            const content: any = {
                              summary: String(c.summary ?? ""),
                              key_points: clone(c.key_points),
                              decisions: clone(c.decisions),
                              action_items: clone(c.action_items),
                              risks: clone(c.risks),
                              open_questions: clone(c.open_questions),
                              participants: clone(c.participants),
                              auto_speakers_unreviewed: !!c.auto_speakers_unreviewed,
                            };
                            // 双保险：即使编辑器漏了，也不把读取时派生的键写进版本快照
                            for (const k of ["key_points", "decisions", "risks", "open_questions", "action_items"]) {
                              content[k] = content[k].map((it: any) => {
                                if (!it || typeof it !== "object") return it;
                                const { evidence, evidence_status, vector_matched, ...rest } = it;
                                return rest;
                              });
                            }
                            content.participants = content.participants.map((p: any) => {
                              if (!p || typeof p !== "object") return p;
                              const { segment_count, ...rest } = p;
                              return rest;
                            });
                            const saved = await api<any>(`/api/meetings/${id}/report`, {
                              method: "PATCH",
                              body: JSON.stringify({ content_json: content }),
                            });
                            reload();
                            setRepEditing(false);
                            setRepDraft(null);
                            setFlash(`已保存为第 ${saved?.version ?? "?"} 版草稿`);
                          })
                        }
                      >
                        {busy === "save_report" ? "保存中…" : "保存"}
                      </button>{" "}
                      <button
                        type="button"
                        onClick={() => {
                          setRepEditing(false);
                          setRepDraft(null);
                        }}
                      >
                        取消
                      </button>
                    </>
                  ) : (
                    <>
                      <ReportView
                        content={report.current.content}
                        segs={segs}
                        seek={seek}
                        meetingId={id}
                        onAsk={askAbout}
                      />
                      <button
                        type="button"
                        disabled={busy === "report"}
                        onClick={() => {
                          setRepDraft(JSON.parse(JSON.stringify(report.current.content || {})));
                          setRepEditing(true);
                        }}
                        title="编辑会产生新草稿版本"
                      >
                        编辑
                      </button>
                    </>
                  )}
                  {!repEditing && report.current.status === "draft" && (
                    <button
                      type="button"
                      disabled={busy === "accept"}
                      onClick={() =>
                        run("accept", async () => {
                          await api(`/api/report-versions/${report.current.id}/accept`, { method: "POST" });
                          reload();
                        })
                      }
                    >
                      {busy === "accept" ? "接受中…" : "接受"}
                    </button>
                  )}
                </div>
              )}
            </div>
          )}
          {tab === "qa" && (
            <div className="card">
              <p className="cloud">问答会把检索到的片段发给 LLM。</p>
              <label className="muted">范围</label>
              <select
                value={qaScope}
                style={{ width: 220 }}
                onChange={(e) => {
                  const v = e.target.value === "group" ? "group" : "meeting";
                  setQaScope(v);
                  try {
                    sessionStorage.setItem("huiyiku-qa-scope", v);
                  } catch {
                    /* ignore */
                  }
                }}
              >
                <option value="meeting">本场会议</option>
                <option value="group">当前会议组</option>
              </select>
              <p />
              <div className="row" style={{ flexWrap: "wrap", gap: 8 }}>
                <select aria-label="按内容类型筛选" value={qaType} style={{ width: 150 }} onChange={(e) => setQaType(e.target.value)}>
                  <option value="">全部类型</option>
                  <option value="transcript">仅转写</option>
                  <option value="decision">决议</option>
                  <option value="action">行动项</option>
                  <option value="risk">风险</option>
                  <option value="summary">摘要</option>
                  <option value="question">待跟进问题</option>
                </select>
                <select aria-label="按人物筛选" value={qaPerson} style={{ width: 140 }} onChange={(e) => setQaPerson(Number(e.target.value))} title={qaType && qaType !== "transcript" ? "人物筛选仅作用于转写片段" : ""}>
                  <option value={0}>全部人物</option>
                  {persons.map((p) => (
                    <option key={p.id} value={p.id}>
                      {p.name}
                    </option>
                  ))}
                </select>
                <input type="date" aria-label="会议日期不早于" value={qaFrom} style={{ width: 150 }} onChange={(e) => setQaFrom(e.target.value)} title="会议日期不早于" />
                <span className="muted">至</span>
                <input type="date" aria-label="会议日期不晚于" value={qaTo} style={{ width: 150 }} onChange={(e) => setQaTo(e.target.value)} title="会议日期不晚于" />
              </div>
              <p />
              <label className="muted" style={{ display: "flex", gap: 8, alignItems: "center" }}>
                <input
                  type="checkbox"
                  checked={qaExtra}
                  onChange={(e) => setQaExtra(e.target.checked)}
                />
                允许补充录音之外的知识（会单独标注为 AI 补充，不作为会议事实）
              </label>
              <p />
              <button
                type="button"
                disabled={busy === "qa"}
                onClick={startThread}
                title="结束当前话题，开新会话（历史保留在库里）"
              >
                新会话
              </button>
              {qaTurns.length > 0 && threadId && (
                <>
                  <a
                    className="muted"
                    href={`/api/export/qa/${threadId}.md`}
                    download
                    title="分享前请确认已获参会人同意"
                  >
                    导出会话 MD
                  </a>{" "}
                  <a className="muted" href={`/api/export/qa/${threadId}.html`} download>
                    HTML
                  </a>
                </>
              )}
              <textarea
                aria-label="问题"
                ref={qaInputRef}
                value={qaQ}
                onChange={(e) => setQaQ(e.target.value)}
                rows={3}
              />
              <p />
              <button
                type="button"
                className="primary"
                disabled={busy === "qa" || !qaQ.trim()}
                onClick={() =>
                  run("qa", async () => {
                    const scope: Record<string, unknown> =
                      qaScope === "group" && m?.group_id
                        ? { group_ids: [m.group_id] }
                        : { meeting_ids: [id] };
                    if (qaType) scope.chunk_types = [qaType];
                    if (qaFrom) scope.start = qaFrom;
                    if (qaFrom && qaTo && qaFrom > qaTo) {
                      setFlash("日期范围无效：开始不能晚于结束");
                      return;
                    }
                    if (qaTo) scope.end = qaTo + "T23:59:59";
                    if (qaPerson) scope.person_id = qaPerson;
                    // 范围快照比对：不一致自动开新线程（历史留在库里）
                    const key = JSON.stringify(scope);
                    let tid = threadId;
                    if (threadScope && key !== threadScope) {
                      tid = crypto.randomUUID();
                      try {
                        localStorage.setItem(`huiyiku-qa-thread-${id}`, tid);
                      } catch {
                        /* ignore */
                      }
                      setThreadId(tid);
                      setQaTurns([]);
                      setFlash("范围已变化，已开新会话");
                    }
                    setThreadScope(key);
                    setQaTurns((xs) => [...xs, { q: qaQ, a: "", x: "", cites: [] }]);
                    const done: any = await readSse(
                      "/api/qa",
                      { session_id: tid, question: qaQ, scope, allow_extra: qaExtra },
                      (t, kind) => {
                        setQaTurns((xs) => {
                          if (!xs.length) return xs;
                          const next = xs.slice();
                          const last = { ...next[next.length - 1] };
                          if (kind === "extra") last.x += t;
                          else last.a += t;
                          next[next.length - 1] = last;
                          return next;
                        });
                      },
                    );
                    setQaTurns((xs) => {
                      if (!xs.length) return xs;
                      const next = xs.slice();
                      const last = { ...next[next.length - 1] };
                      if (done?.citations) last.cites = done.citations;
                      next[next.length - 1] = last;
                      return next;
                    });
                    setQaQ("");
                  })
                }
              >
                {busy === "qa" ? "回答中…" : "提问"}
              </button>
              {qaTurns.map((t, i) => {
                const last = i === qaTurns.length - 1;
                return (
                  <div className="qa-turn" key={i}>
                    <div className="qa-turn-q">{t.q}</div>
                    <div style={{ whiteSpace: "pre-wrap" }}>{t.a}</div>
                    {t.x && (
                      <div className="qa-extra">
                        <p className="muted">AI 补充（非录音内容，不作为会议事实）</p>
                        <div style={{ whiteSpace: "pre-wrap" }}>{t.x}</div>
                      </div>
                    )}
                    {last ? (
                      t.cites.map((c, j) => {
                        const isReport = c.chunk_type && c.chunk_type !== "transcript";
                        return (
                          <div
                            key={j}
                            className={"qa-cite" + (c.verified === false ? " warn" : "")}
                          >
                            {isReport ? (
                              <span className="tag tag-mid">报告条目</span>
                            ) : (
                              <button
                                type="button"
                                onClick={() => {
                                  const hit = segs.find(
                                    (s) => Math.abs(s.start_ms - c.start_ms) < 80,
                                  );
                                  seek(c.start_ms, hit?.id);
                                }}
                              >
                                {fmt(c.start_ms)}
                                {c.speaker_name ? ` · ${c.speaker_name}` : ""}
                              </button>
                            )}{" "}
                            {c.verified === false ? "引用未通过原文校验" : ""}
                            <div className="muted">{c.text}</div>
                          </div>
                        );
                      })
                    ) : t.cites.length > 0 ? (
                      <p className="muted">引用 {t.cites.length} 条</p>
                    ) : null}
                  </div>
                );
              })}
            </div>
          )}
          {tab === "people" && <PeopleBox meetingId={id} />}
        </div>
      </div>
    </div>
  );
}

function PeopleBox({ meetingId }: { meetingId: number }) {
  const [persons, setPersons] = useState<any[]>([]);
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");
  const load = () => {
    api<any[]>("/api/persons").then(setPersons);
  };
  useEffect(load, []);
  return (
    <div className="card">
      <h3>名册</h3>
      <p className="muted">把会议与人名关联后，问答可按人物过滤。</p>
      <div className="row">
        <input value={name} onChange={(e) => setName(e.target.value)} placeholder="姓名" />
        <button
          type="button"
          disabled={busy || !name.trim()}
          onClick={async () => {
            setBusy(true);
            setMsg("");
            try {
              const p = await api<any>("/api/persons", { method: "POST", body: JSON.stringify({ name }) });
              await api(`/api/meetings/${meetingId}/participants?person_id=${p.id}`, { method: "POST" });
              setName("");
              load();
            } catch (e) {
              setMsg(String(e));
            } finally {
              setBusy(false);
            }
          }}
        >
          {busy ? "添加中…" : "添加"}
        </button>
      </div>
      {msg && <p className="err">{msg}</p>}
      {persons.length === 0 && <p className="muted">还没有人名。先添加一位参会人。</p>}
      {persons.map((p) => (
        <div key={p.id}>{p.name}</div>
      ))}
    </div>
  );
}

function ActionsPage() {
  const [items, setItems] = useState<any[]>([]);
  const [status, setStatus] = useState("");
  const load = (s: string) => {
    const q = s ? `?status=${encodeURIComponent(s)}` : "";
    api<any[]>(`/api/actions${q}`).then(setItems);
  };
  useEffect(() => {
    load(status);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status]);
  const csvQuery = status ? `?status=${encodeURIComponent(status)}` : "";
  return (
    <div>
      <div className="row">
        <h2 style={{ margin: 0 }}>行动项</h2>
        <select aria-label="按状态筛选" value={status} style={{ width: 130 }} onChange={(e) => setStatus(e.target.value)}>
          <option value="">全部状态</option>
          {["open", "doing", "blocked", "done", "cancelled"].map((s) => (
            <option key={s}>{s}</option>
          ))}
        </select>
        <a
          className="muted"
          href={`/api/export/actions.csv${csvQuery}`}
          download
          title="CSV 可直接用 Excel 打开，含出处（时间点/人物/原句）"
        >
          导出 CSV
        </a>
      </div>
      {items.length === 0 && <div className="empty">接受会议报告后会抽出行动项。</div>}
      {items.map((a) => (
        <div key={a.id} className="card row">
          <div style={{ flex: 1 }}>{a.title}</div>
          <select
            value={a.status}
            style={{ width: 120 }}
            onChange={(e) => {
              const next = e.target.value;
              api(`/api/actions/${a.id}`, { method: "PATCH", body: JSON.stringify({ status: next }) })
                .then(() =>
                  setItems((xs) =>
                    // 正在按状态筛选时，改完不再匹配就立刻移出列表
                    status && next !== status
                      ? xs.filter((x) => x.id !== a.id)
                      : xs.map((x) => (x.id === a.id ? { ...x, status: next } : x)),
                  ),
                )
                .catch(() => load(status));
            }}
          >
            {["open", "doing", "blocked", "done", "cancelled"].map((s) => (
              <option key={s}>{s}</option>
            ))}
          </select>
        </div>
      ))}
    </div>
  );
}

function SettingsPage() {
  const [hotwords, setHotwords] = useState("");
  const [busy, setBusy] = useState("");
  const [err, setErr] = useState("");
  const [shareOn, setShareOn] = useState(false);
  const [lanUrls, setLanUrls] = useState<string[]>([]);
  const [shareErr, setShareErr] = useState("");
  const [llm, setLlm] = useState<LlmInfo | null>(null);
  const [ep, setEp] = useState("");
  const [mn, setMn] = useState("");
  const [key, setKey] = useState("");
  const [models, setModels] = useState<string[]>([]);
  const [keyConfigured, setKeyConfigured] = useState(false);
  const [llmMsg, setLlmMsg] = useState("");
  const [llmErr, setLlmErr] = useState("");
  const [hwMsg, setHwMsg] = useState("");
  const [hwErr, setHwErr] = useState("");
  const [emb, setEmb] = useState<LlmInfo | null>(null);
  const [embEp, setEmbEp] = useState("");
  const [embMn, setEmbMn] = useState("");
  const [embKey, setEmbKey] = useState("");
  const [embModels, setEmbModels] = useState<string[]>([]);
  const [embKeyConfigured, setEmbKeyConfigured] = useState(false);
  const [embMsg, setEmbMsg] = useState("");
  const [embErr, setEmbErr] = useState("");
  const loadSettings = () => {
    api<{
      hotwords?: string;
      llm_api_key_configured?: boolean;
      embedding_api_key_configured?: boolean;
      share_lan?: boolean;
      lan_urls?: string[];
    }>("/api/settings")
      .then((s) => {
        setHotwords((s.hotwords || "").split(/\s+/).filter(Boolean).join("\n"));
        setKeyConfigured(!!s.llm_api_key_configured);
        setEmbKeyConfigured(!!s.embedding_api_key_configured);
        setShareOn(!!s.share_lan);
        setLanUrls(s.lan_urls || []);
      })
      .catch(() => setErr("读取设置失败"));
  };
  const toggleShare = async () => {
    const next = !shareOn;
    setBusy("share");
    setShareErr("");
    try {
      const s = await api<{ share_lan?: boolean; lan_urls?: string[] }>("/api/settings", {
        method: "PATCH",
        body: JSON.stringify({ share_lan: next }),
      });
      setShareOn(!!s.share_lan);
      setLanUrls(s.lan_urls || []);
      window.dispatchEvent(new CustomEvent("huiyiku-share", { detail: !!s.share_lan }));
    } catch (e) {
      setShareErr(String(e));
    } finally {
      setBusy("");
    }
  };
  const loadEmb = () => {
    api<LlmInfo[]>("/api/models")
      .then((rows) => {
        const candidates = rows.filter((m) => m.task === "embedding");
        const cur = candidates.find((m) => m.is_default) || candidates[0] || null;
        setEmb(cur);
        if (cur) {
          setEmbEp(cur.endpoint || "");
          setEmbMn(cur.model_name || "");
        }
      })
      .catch(() => undefined);
  };
  const loadLlm = () => {
    api<LlmInfo[]>("/api/models")
      .then((rows) => {
        const candidates = rows.filter((m) => m.task === "llm");
        const cur = candidates.find((m) => m.is_default) || candidates[0] || null;
        setLlm(cur);
        if (cur) {
          setEp(cur.endpoint || "");
          setMn(cur.model_name || "");
        }
      })
      .catch(() => undefined);
  };
  useEffect(() => {
    loadSettings();
    loadLlm();
    loadEmb();
  }, []);
  const saveHotwords = async () => {
    setBusy("hotwords");
    setHwErr("");
    setHwMsg("");
    try {
      const joined = hotwords.split(/\s+/).map((x) => x.trim()).filter(Boolean).join(" ");
      await api("/api/settings", { method: "PATCH", body: JSON.stringify({ hotwords: joined }) });
      setHwMsg("热词已保存。对新转写生效；已有会议可在其页面点「重新转写」。");
    } catch (e) {
      setHwErr(String(e));
    } finally {
      setBusy("");
    }
  };
  const saveLlm = async () => {
    setBusy("llm");
    setLlmErr("");
    setLlmMsg("");
    try {
      if (!ep.trim() || !mn.trim()) {
        throw new Error("Base URL 和模型名称必填");
      }
      if (!key.trim() && !keyConfigured) {
        throw new Error("首次配置需要填写 API Key");
      }
      // 重走 setup upsert：更新/新建 LLM 注册行，探测通过自动设为默认；
      // Key 留空时后端沿用已存 Key（换端点/模型无需重填）
      const r = await api<{ ok: boolean; llm_probe_error: string | null }>("/api/setup", {
        method: "POST",
        body: JSON.stringify({
          legal_confirmed: true,
          egress_confirmed: true,
          llm_endpoint: ep.trim(),
          llm_model: mn.trim(),
          llm_api_key: key.trim(),
        }),
      });
      setKey("");
      if (r.llm_probe_error) {
        setLlmErr(`连通测试未通过，配置未改动：${r.llm_probe_error}`);
      } else {
        setLlmMsg("已保存并通过连通测试，报告/问答即刻使用该配置。");
      }
      loadLlm();
      loadSettings();
    } catch (e) {
      setLlmErr(String(e));
    } finally {
      setBusy("");
    }
  };
  const fetchModels = async () => {
    setBusy("models");
    setLlmErr("");
    setLlmMsg("");
    try {
      const r = await api<{ models: string[] }>("/api/llm/models");
      setModels(r.models || []);
      setLlmMsg(`已拉取 ${(r.models || []).length} 个可用模型，点下方模型名即可选中。`);
    } catch (e) {
      setLlmErr(String(e));
    } finally {
      setBusy("");
    }
  };
  const saveEmb = async () => {
    setBusy("emb");
    setEmbErr("");
    setEmbMsg("");
    try {
      if (!embEp.trim() || !embMn.trim()) {
        throw new Error("Base URL 和模型名称必填");
      }
      if (!embKey.trim() && !embKeyConfigured) {
        throw new Error("首次配置需要填写 API Key");
      }
      // 重走 setup upsert：更新/新建向量模型注册行，探测通过自动设为默认；
      // Key 留空时后端沿用已存 Key
      await api<{ ok: boolean; llm_probe_error: string | null }>("/api/setup", {
        method: "POST",
        body: JSON.stringify({
          legal_confirmed: true,
          egress_confirmed: true,
          embedding_endpoint: embEp.trim(),
          embedding_model: embMn.trim(),
          embedding_api_key: embKey.trim(),
        }),
      });
      setEmbKey("");
      setEmbMsg("已保存。报告出处的「AI 补充」条目会尝试用向量匹配定位时间点（标注为时间近似）。");
      loadEmb();
      loadSettings();
    } catch (e) {
      setEmbErr(String(e));
    } finally {
      setBusy("");
    }
  };
  const fetchEmbModels = async () => {
    setBusy("emb_models");
    setEmbErr("");
    setEmbMsg("");
    try {
      const r = await api<{ models: string[] }>("/api/llm/models?task=embedding");
      setEmbModels(r.models || []);
      setEmbMsg(`已拉取 ${(r.models || []).length} 个可用模型，点下方模型名即可选中。`);
    } catch (e) {
      setEmbErr(String(e));
    } finally {
      setBusy("");
    }
  };
  return (
    <div style={{ maxWidth: 640 }}>
      <div className="card">
        <div className="row" style={{ justifyContent: "space-between", alignItems: "center" }}>
          <h2 style={{ margin: 0 }}>内网共享</h2>
          <button
            type="button"
            className="switch"
            role="switch"
            aria-checked={shareOn}
            aria-label={shareOn ? "关闭内网共享" : "开启内网共享"}
            disabled={busy === "share"}
            onClick={() => void toggleShare()}
          >
            <i />
          </button>
        </div>
        {shareOn ? (
          <>
            <p>已开启。可信内网的同事打开下面的地址即可使用，不需要令牌。传输是明文 HTTP。</p>
            {lanUrls.length > 0 ? (
              <ul className="share-urls">
                {lanUrls.map((url) => (
                  <li key={url}>
                    <a href={url}>{url}</a>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="muted">没有检测到局域网地址。同事访问本机的 IPv4，端口 8787。</p>
            )}
          </>
        ) : (
          <p className="muted">已关闭。只有这台电脑能打开会议库。再点一次开关即可给内网同事使用。</p>
        )}
        <p className="muted">若系统弹出防火墙提示，允许专用网络后同事才能连上。关掉开关立即生效，不必重启。</p>
        {shareErr && <p className="err">{shareErr}</p>}
      </div>
      <div className="card">
        <h2>远程 LLM</h2>
        {llm ? (
          <p className="muted">
            当前：{llm.model_name} @ {llm.endpoint}（
            {llm.status === "available" ? "可用" : llm.status === "failed" ? "未通过连通测试" : llm.status}
            {llm.is_default ? " · 默认" : ""}）
          </p>
        ) : (
          <p className="muted">尚未配置远程 LLM。</p>
        )}
        <p className="muted">
          仅支持 OpenAI-compatible Chat Completions 端点。换配置后点「保存并测试」，
          通过连通测试才会设为默认。
        </p>
        <label className="muted" htmlFor="cfg-llm-ep">Base URL（API 端点）</label>
        <input id="cfg-llm-ep" value={ep} onChange={(e) => setEp(e.target.value)} placeholder="https://api.deepseek.com" />
        <p />
        <label className="muted" htmlFor="cfg-llm-mn">模型名称</label>
        <input
          id="cfg-llm-mn"
          list="llm-model-options"
          value={mn}
          onChange={(e) => setMn(e.target.value)}
          placeholder="deepseek-chat"
        />
        <datalist id="llm-model-options">
          {models.map((m) => (
            <option key={m} value={m} />
          ))}
        </datalist>
        <button type="button" disabled={!!busy} onClick={() => void fetchModels()}>
          {busy === "models" ? "拉取中…" : "拉取可用模型"}
        </button>
        {models.length > 0 && (
          <>
            <p className="muted">点一个模型名即可填入上方输入框：</p>
            <div className="row">
              {models.map((m) => (
                <button
                  type="button"
                  key={m}
                  className={m === mn.trim() ? "primary" : ""}
                  onClick={() => setMn(m)}
                >
                  {m}
                </button>
              ))}
            </div>
          </>
        )}
        <p />
        <label className="muted" htmlFor="cfg-llm-key">
          {keyConfigured ? "API Key（已配置；换服务商才需重填）" : "API Key（首次必填）"}
        </label>
        <input
          id="cfg-llm-key"
          type="password"
          value={key}
          onChange={(e) => setKey(e.target.value)}
          placeholder={keyConfigured ? "留空则沿用已存 Key" : "sk-..."}
        />
        <p />
        <button type="button" className="primary" disabled={!!busy} onClick={() => void saveLlm()}>
          {busy === "llm" ? "测试并保存中…" : "保存并测试"}
        </button>
        {llmMsg && <p className="ok">{llmMsg}</p>}
        {llmErr && <p className="err">{llmErr}</p>}
      </div>
      <div className="card">
        <h2>向量模型（可选）</h2>
        {emb ? (
          <p className="muted">
            当前：{emb.model_name} @ {emb.endpoint}（
            {emb.status === "available" ? "可用" : emb.status === "failed" ? "未通过连通测试" : emb.status}
            {emb.is_default ? " · 默认" : ""}）
          </p>
        ) : (
          <p className="muted">尚未配置。不配置也能正常使用全部功能。</p>
        )}
        <p className="muted">
          用途：报告出处里「AI 补充·录音未找到」的条目，改用语义相似度在转写里找时间点相近的原文
          （标注为时间近似）；也用于语义检索。隐私：开启后会把转写文本片段发给该端点。
          仅支持 OpenAI-compatible /embeddings 端点。
        </p>
        <label className="muted" htmlFor="cfg-emb-ep">Base URL（API 端点）</label>
        <input id="cfg-emb-ep" value={embEp} onChange={(e) => setEmbEp(e.target.value)} placeholder="https://api.siliconflow.cn" />
        <p />
        <label className="muted" htmlFor="cfg-emb-mn">模型名称</label>
        <input
          id="cfg-emb-mn"
          list="emb-model-options"
          value={embMn}
          onChange={(e) => setEmbMn(e.target.value)}
          placeholder="BAAI/bge-m3"
        />
        <datalist id="emb-model-options">
          {embModels.map((m) => (
            <option key={m} value={m} />
          ))}
        </datalist>
        <button type="button" disabled={!!busy} onClick={() => void fetchEmbModels()}>
          {busy === "emb_models" ? "拉取中…" : "拉取可用模型"}
        </button>
        {embModels.length > 0 && (
          <>
            <p className="muted">点一个模型名即可填入上方输入框：</p>
            <div className="row">
              {embModels.map((m) => (
                <button
                  type="button"
                  key={m}
                  className={m === embMn.trim() ? "primary" : ""}
                  onClick={() => setEmbMn(m)}
                >
                  {m}
                </button>
              ))}
            </div>
          </>
        )}
        <p />
        <label className="muted" htmlFor="cfg-emb-key">
          {embKeyConfigured ? "API Key（已配置；换服务商才需重填）" : "API Key（首次必填）"}
        </label>
        <input
          id="cfg-emb-key"
          type="password"
          value={embKey}
          onChange={(e) => setEmbKey(e.target.value)}
          placeholder={embKeyConfigured ? "留空则沿用已存 Key" : "sk-..."}
        />
        <p />
        <button type="button" className="primary" disabled={!!busy} onClick={() => void saveEmb()}>
          {busy === "emb" ? "测试并保存中…" : "测试并保存"}
        </button>
        {embMsg && <p className="ok">{embMsg}</p>}
        {embErr && <p className="err">{embErr}</p>}
      </div>
      <div className="card">
        <h2>转写热词</h2>
        <p className="muted">
          路名、人名、产品名、术语等专有名词，每行一个。FunASR 会优先识别这些词，
          显著减少专名被转错的情况。对新转写生效。
        </p>
        <textarea
          aria-label="转写热词"
          value={hotwords}
          onChange={(e) => setHotwords(e.target.value)}
          rows={6}
          placeholder={"某某路\n张三\n某某产品名"}
        />
        <p />
        <button type="button" className="primary" disabled={!!busy} onClick={() => void saveHotwords()}>
          {busy === "hotwords" ? "保存中…" : "保存"}
        </button>
        {hwMsg && <p className="ok">{hwMsg}</p>}
        {hwErr && <p className="err">{hwErr}</p>}
      </div>
      {err && <p className="err">{err}</p>}
    </div>
  );
}

function DigestPage() {
  const [groups, setGroups] = useState<any[]>([]);
  const [gid, setGid] = useState<number | "">("");
  const [items, setItems] = useState<any[]>([]);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState("");
  const [msg, setMsg] = useState("");
  useEffect(() => {
    api<any[]>("/api/groups").then((g) => {
      setGroups(g);
      if (g[0]) setGid(g[0].id);
    });
  }, []);
  useEffect(() => {
    if (gid) api<any[]>(`/api/groups/${gid}/digests`).then(setItems).catch(() => setItems([]));
  }, [gid]);
  return (
    <div>
      <h2>项目进展</h2>
      <p className="cloud">基于已接受报告，会把文本发给 LLM。</p>
      <div className="row">
        <select value={gid} onChange={(e) => setGid(Number(e.target.value))} style={{ width: 220 }}>
          {groups.map((g) => (
            <option key={g.id} value={g.id}>{g.name}</option>
          ))}
        </select>
        {gid ? (
          <>
            <a className="muted" href={`/api/export/group/${gid}.md`} download title="各场报告含出处的汇总">
              导出汇总 MD
            </a>
            <a className="muted" href={`/api/export/group/${gid}.html`} download title="浏览器打开后打印可存 PDF">
              HTML
            </a>
            <a className="muted" href={`/api/export/group/${gid}.docx`} download>
              Word
            </a>
          </>
        ) : null}
      </div>
      <button
        type="button"
        className="primary"
        disabled={!gid || busy}
        onClick={async () => {
          setBusy(true);
          setMsg("");
          setNote("");
          try {
            const res = await api<{ job_id: number }>(`/api/groups/${gid}/digest`, {
              method: "POST",
              body: JSON.stringify({}),
            });
            if (res.job_id) await waitJob(res.job_id, setNote);
            if (gid) setItems(await api<any[]>(`/api/groups/${gid}/digests`));
          } catch (e) {
            setMsg(String(e));
          } finally {
            setBusy(false);
            setNote("");
          }
        }}
      >
        {busy ? "生成中…" : "生成摘要"}
      </button>
      {note && <p className="muted">{note}</p>}
      {msg && <p className="err">{msg}</p>}
      {items.length === 0 && <div className="empty">先接受至少一份会议报告，再生成项目进展。</div>}
      {items.map((d) => (
        <div key={d.id} className="card">
          <div className="muted">{d.created_at}</div>
          <pre className="report-body">{d.content_md || reportText(d.content)}</pre>
        </div>
      ))}
    </div>
  );
}

function fmt(ms: number) {
  const s = Math.floor(ms / 1000);
  const m = Math.floor(s / 60);
  const h = Math.floor(m / 60);
  return `${String(h).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
}

function fmtDate(iso: string) {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

// 导航图标（16px 线性 SVG）
function Ico({ d }: { d: string }) {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d={d} />
    </svg>
  );
}
const IcoChat = () => <Ico d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" />;
const IcoImport = () => <Ico d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3" />;
const IcoCheck = () => <Ico d="M9 11l3 3L22 4M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11" />;
const IcoTrend = () => <Ico d="M23 6l-9.5 9.5-5-5L1 18M17 6h6v6" />;
const IcoReload = () => <Ico d="M23 4v6h-6M1 20v-6h6M3.51 9a9 9 0 0114.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0020.49 15" />;
const IcoGear = () => (
  <Ico d="M12 15a3 3 0 100-6 3 3 0 000 6zM19.4 15a1.65 1.65 0 00.33 1.82l.06.06a2 2 0 11-2.83 2.83l-.06-.06a1.65 1.65 0 00-1.82-.33 1.65 1.65 0 00-1 1.51V21a2 2 0 11-4 0v-.09A1.65 1.65 0 008.6 19.4a1.65 1.65 0 00-1.82.33l-.06.06a2 2 0 11-2.83-2.83l.06-.06a1.65 1.65 0 00.33-1.82 1.65 1.65 0 00-1.51-1H3a2 2 0 110-4h.09A1.65 1.65 0 004.6 8.6a1.65 1.65 0 00-.33-1.82l-.06-.06a2 2 0 112.83-2.83l.06.06a1.65 1.65 0 001.82.33H9a1.65 1.65 0 001-1.51V3a2 2 0 114 0v.09a1.65 1.65 0 001 1.51 1.65 1.65 0 001.82-.33l.06-.06a2 2 0 112.83 2.83l-.06.06a1.65 1.65 0 00-.33 1.82V9a1.65 1.65 0 001.51 1H21a2 2 0 110 4h-.09a1.65 1.65 0 00-1.51 1z" />
);

const EV_LABEL: Record<string, { text: string; cls: string }> = {
  quoted: { text: "录音原文", cls: "tag-ok" },
  paraphrase: { text: "释义", cls: "tag-mid" },
  unsupported: { text: "AI 补充·录音未找到", cls: "tag-warn" },
};

const REPORT_SECTIONS: { key: string; title: string; field?: string }[] = [
  { key: "key_points", title: "重点" },
  { key: "decisions", title: "决议" },
  { key: "action_items", title: "行动项", field: "title" },
  { key: "risks", title: "风险" },
  { key: "open_questions", title: "待跟进" },
];

// 报告结构化视图：每条带出处（时间点 · 人物），点击跳播放、展开看原句
function ReportView({
  content,
  segs,
  seek,
  meetingId,
  onAsk,
}: {
  content: any;
  segs: any[];
  seek: (ms: number, segId?: number) => void;
  meetingId: number;
  onAsk: (text: string) => void;
}) {
  if (!content || typeof content !== "object") {
    return <pre className="report-body">{String(content ?? "")}</pre>;
  }
  const summary = content.evidence_summary;
  const total = summary ? summary.quoted + summary.paraphrase + summary.unsupported : 0;
  const names = (ev: any[]) => {
    const ns = Array.from(new Set(ev.map((e) => e.speaker_name).filter(Boolean))) as string[];
    return ns.length ? ` · ${ns.slice(0, 2).join("、")}${ns.length > 2 ? "等" : ""}` : "";
  };
  const jump = (ms: number) => {
    const hit = segs.find((s) => Math.abs(s.start_ms - ms) < 80);
    seek(ms, hit?.id);
  };
  return (
    <div className="report-view">
      {content.auto_speakers_unreviewed && (
        <p className="muted">说话人尚未人工校对，出处里的人物名为机器标注。</p>
      )}
      {summary && total > 0 && (
        <p className="muted">
          出处核对：{total} 条中 {summary.unsupported} 条未在录音中定位到原文
          {Array.isArray(summary.span_ms) && `；录音覆盖 ${fmt(summary.span_ms[0])}–${fmt(summary.span_ms[1])}`}
        </p>
      )}
      {content.summary ? (
        <div className="report-sec">
          <h3>摘要</h3>
          <p className="report-summary">{String(content.summary)}</p>
        </div>
      ) : null}
      {REPORT_SECTIONS.map((sec) => {
        const items = content[sec.key];
        if (!Array.isArray(items) || items.length === 0) return null;
        return (
          <div className="report-sec" key={sec.key}>
            <h3>{sec.title}</h3>
            <ul className="report-items">
              {items.map((it: any, i: number) => {
                if (!it || typeof it !== "object") {
                  return <li key={i}>{String(it)}</li>;
                }
                const text = it[sec.field || "content"] || it.title || JSON.stringify(it);
                const st = it.evidence_status as string | undefined;
                const ev: any[] = Array.isArray(it.evidence) ? it.evidence : [];
                return (
                  <ReportItem
                    key={i}
                    text={text}
                    status={st}
                    evidence={ev}
                    names={names}
                    jump={jump}
                    meetingId={meetingId}
                    onAsk={onAsk}
                  />
                );
              })}
            </ul>
          </div>
        );
      })}
      {Array.isArray(content.participants) && content.participants.length > 0 && (
        <div className="report-sec">
          <h3>参会人</h3>
          <p className="muted">
            {content.participants
              .map((p: any) =>
                `${p?.name ?? "?"}${p?.segment_count ? `（发言 ${p.segment_count} 段）` : ""}`
              )
              .join("、")}
          </p>
        </div>
      )}
    </div>
  );
}

// 报告逐条编辑器：改文本 / 删条目 / 加条目；行动项带负责人·截止·优先级
function ReportEditor({
  draft,
  persons,
  onChange,
}: {
  draft: any;
  persons: any[];
  onChange: (next: any) => void;
}) {
  const c = draft || {};
  const setSec = (key: string, items: any[]) => onChange({ ...c, [key]: items });
  const setItem = (key: string, i: number, patch: any) => {
    setSec(key, (c[key] || []).map((it: any, j: number) => (j === i ? { ...it, ...patch } : it)));
  };
  const delItem = (key: string, i: number) => {
    setSec(key, (c[key] || []).filter((_: any, j: number) => j !== i));
  };
  return (
    <div className="report-editor">
      <h3>摘要</h3>
      <textarea
        aria-label="编辑摘要"
        value={String(c.summary ?? "")}
        rows={4}
        style={{ width: "100%" }}
        onChange={(e) => onChange({ ...c, summary: e.target.value })}
      />
      {REPORT_SECTIONS.map((sec) => {
        const items = Array.isArray(c[sec.key]) ? c[sec.key] : [];
        const field = sec.field || "content";
        return (
          <div className="report-sec" key={sec.key}>
            <h3>{sec.title}</h3>
            {items.map((it: any, i: number) => (
              <div className="report-edit-item" key={i} style={{ marginBottom: 8 }}>
                <textarea
                  aria-label={`编辑${sec.title}第${i + 1}条`}
                  value={String(it?.[field] ?? "")}
                  rows={2}
                  style={{ width: "100%" }}
                  onChange={(e) => setItem(sec.key, i, { [field]: e.target.value })}
                />
                {sec.key === "action_items" && (
                  <div className="row" style={{ flexWrap: "wrap", gap: 8 }}>
                    <select
                      aria-label="负责人"
                      value={it?.owner_person_id ?? ""}
                      style={{ width: 140 }}
                      onChange={(e) =>
                        setItem(sec.key, i, {
                          owner_person_id: e.target.value === "" ? null : Number(e.target.value),
                        })
                      }
                    >
                      <option value="">未指定</option>
                      {persons.map((p) => (
                        <option key={p.id} value={p.id}>
                          {p.name}
                        </option>
                      ))}
                    </select>
                    <input
                      type="date"
                      aria-label="截止日期"
                      value={String(it?.due_at ?? "").slice(0, 10)}
                      onChange={(e) => setItem(sec.key, i, { due_at: e.target.value || null })}
                    />
                    <select
                      aria-label="优先级"
                      value={it?.priority ?? ""}
                      style={{ width: 110 }}
                      onChange={(e) => setItem(sec.key, i, { priority: e.target.value || null })}
                    >
                      <option value="">默认</option>
                      <option value="normal">normal</option>
                      <option value="high">high</option>
                      <option value="low">low</option>
                    </select>
                  </div>
                )}
                <button type="button" className="linklike" onClick={() => delItem(sec.key, i)}>
                  删除
                </button>
              </div>
            ))}
            <button
              type="button"
              className="linklike"
              onClick={() =>
                setSec(sec.key, [
                  ...items,
                  sec.key === "action_items"
                    ? { title: "", owner_person_id: null, status: "open", segment_ids: [] }
                    : { [field]: "", segment_ids: [], time_ms: null },
                ])
              }
            >
              添加一条
            </button>
          </div>
        );
      })}
      <div className="report-sec">
        <h3>参会人</h3>
        <p className="muted">
          {(c.participants || [])
            .map((p: any) => p?.name ?? "?")
            .join("、") || "（无）"}
        </p>
      </div>
    </div>
  );
}

function ReportItem({
  text,
  status,
  evidence,
  names,
  jump,
  meetingId,
  onAsk,
}: {
  text: string;
  status?: string;
  evidence: any[];
  names: (ev: any[]) => string;
  jump: (ms: number) => void;
  meetingId: number;
  onAsk: (text: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [verifying, setVerifying] = useState(false);
  const [verr, setVerr] = useState("");
  const [vr, setVr] = useState<any>(null);
  const label = status ? EV_LABEL[status] : undefined;
  const runVerify = async () => {
    setVerifying(true);
    setVerr("");
    setVr(null);
    try {
      const r = await api<any>("/api/report-items/verify", {
        method: "POST",
        body: JSON.stringify({ meeting_id: meetingId, text }),
      });
      setVr(r);
    } catch (e) {
      setVerr(String(e));
    } finally {
      setVerifying(false);
    }
  };
  return (
    <li className="report-item">
      <div className="report-item-head">
        <span className="report-item-text">{text}</span>
        {label && <span className={`tag ${label.cls}`}>{label.text}</span>}
      </div>
      <div className="report-item-ev">
        {evidence.length > 0 ? (
          <>
            <button type="button" className="ev-chip" onClick={() => jump(evidence[0].start_ms)}>
              〔{fmt(evidence[0].start_ms)}{names(evidence)}〕
            </button>
            <button type="button" className="linklike" onClick={() => setOpen(!open)}>
              {open ? "收起原句" : `原句 ${evidence.length} 段`}
            </button>
            {evidence.some((e: any) => e.vector_matched) && (
              <span className="muted">向量匹配（时间近似）</span>
            )}
          </>
        ) : (
          <span className="muted">未定位到原文</span>
        )}
        <button type="button" className="linklike" disabled={verifying} onClick={() => void runVerify()}>
          {verifying ? "核实中…" : "核实"}
        </button>
        <button type="button" className="linklike" onClick={() => onAsk(text)}>
          就这条追问
        </button>
      </div>
      {verr && <p className="err">{verr}</p>}
      {vr && (
        <div className={"verify-box" + (vr.verdict === "unsupported" ? " warn" : "")}>
          <p>
            核实结论：{vr.verdict_label}
            {vr.reason ? ` — ${vr.reason}` : ""}
          </p>
          {(vr.citations || []).slice(0, 3).map((c: any, j: number) => (
            <div className="ev-line" key={j}>
              <button type="button" onClick={() => jump(c.start_ms)}>{fmt(c.start_ms)}</button>{" "}
              <span className="muted">{c.speaker_name || "?"}：</span>
              {c.text}
            </div>
          ))}
        </div>
      )}
      {open && (
        <div className="ev-list">
          {evidence.map((e, j) => (
            <div className="ev-line" key={j}>
              <button type="button" onClick={() => jump(e.start_ms)}>{fmt(e.start_ms)}</button>{" "}
              <span className="muted">{e.speaker_name || "?"}：</span>
              {e.text}
            </div>
          ))}
        </div>
      )}
    </li>
  );
}

// 把报告 JSON 渲染成可读文本（摘要 + 分节列表）；非对象则原样
function reportText(content: any): string {
  if (!content || typeof content !== "object") return String(content ?? "");
  const lines: string[] = [];
  const pushList = (title: string, items: any[] | undefined) => {
    if (!Array.isArray(items) || items.length === 0) return;
    lines.push(`\n【${title}】`);
    for (const it of items) lines.push(`· ${it?.content ?? it?.title ?? JSON.stringify(it)}`);
  };
  if (content.summary) lines.push(String(content.summary));
  pushList("重点", content.key_points);
  pushList("决议", content.decisions);
  pushList("行动项", content.action_items);
  pushList("风险", content.risks);
  pushList("待跟进", content.open_questions);
  return lines.join("\n") || JSON.stringify(content, null, 2);
}

const STATUS_LABEL: Record<string, { text: string; cls: string }> = {
  created: { text: "待处理", cls: "" },
  preprocessing: { text: "预处理", cls: "accent" },
  transcribing: { text: "转写中", cls: "accent" },
  speaker_review: { text: "待校对", cls: "warn" },
  indexing: { text: "索引中", cls: "accent" },
  indexed: { text: "已入库", cls: "ok" },
  report_draft: { text: "报告草稿", cls: "warn" },
  ready: { text: "就绪", cls: "ok" },
  failed: { text: "失败", cls: "err" },
};

function StatusBadge({ status }: { status: string }) {
  const s = STATUS_LABEL[status] || { text: status, cls: "" };
  return <span className={"badge" + (s.cls ? " " + s.cls : "")}>{s.text}</span>;
}

function stepIndex(status: string) {
  switch (status) {
    case "created":
      return -1;
    case "preprocessing":
      return 0;
    case "transcribing":
      return 1;
    case "speaker_review":
      return 2;
    case "indexing":
      return 3;
    case "indexed":
    case "report_draft":
    case "ready":
      return 4;
    case "failed":
      return -1;
    default:
      return 0;
  }
}
