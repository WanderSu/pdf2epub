import { useEffect, useRef, useState, type ReactNode } from "react";
import { invoke } from "@tauri-apps/api/core";
import { open } from "@tauri-apps/plugin-dialog";
import { T, CLEAN_GROUPS, type Lang } from "../i18n";
import { Rule, Switch, Segment, RadioRow, ChapterHead, ToggleSwitch } from "../primitives";
import {
  CLEAN_KEYS, type BackendPref, type CleanKey, type CleanOpts, type EnvState,
} from "../types";

// ── SETTINGS SCREEN(规范页)───────────────────────────────────────────────────

/** 设置快照:未保存项数 = 与快照的差异个数(不是「改了几次」),DISCARD 也用它回滚 */
export interface SettingsSnapshot {
  backendPref: BackendPref;
  outputDir: string;
  cliPath: string;
  cleanOpts: CleanOpts;
  strictVerify: boolean;
}

/* 以下三个组件必须是模块级定义:定义在 SettingsScreen 内部会在每次渲染时产生
   新的组件类型,React 会卸载重建整棵子树 —— 输入框输一个字符就失焦 */
function SectionCard({ lang, letter, title, summary, children }: {
  lang: Lang; letter: string; title: string; summary: string; children: ReactNode;
}) {
  return (
    <div className="mb-0">
      <div className="flex items-baseline gap-4 py-3 border-b border-[var(--border)]">
        <span className="font-mono text-[11px] tracking-[0.1em] text-[var(--muted-foreground)]">{letter}</span>
        <span className={`text-[20px] text-[var(--foreground)] flex-1 ${lang === "zh" ? "cjk-serif" : "font-serif"}`}>{title}</span>
        <span className="font-mono text-[11px] text-[var(--muted-foreground)]">{summary}</span>
      </div>
      <div className="py-4 space-y-0">{children}</div>
    </div>
  );
}

function FieldRow({ lang, label, children }: { lang: Lang; label: string; children: ReactNode }) {
  return (
    <div className="flex items-center gap-4 py-2 border-b border-[var(--border)]">
      <span className={`text-[13px] text-[var(--muted-foreground)] w-40 shrink-0 ${lang === "zh" ? "cjk" : ""}`}>{label}</span>
      {children}
    </div>
  );
}

/** 凭证行:只写不读(原值不回显,这是项目的凭证约定),SHOW 仅在已输入内容时可点。
 *  CLEAR 需要点两次:已存的密钥抹掉就再也取不回来,一次误点代价太大。 */
function TokenRow({ lang, label, configured, pending, value, onChange, onClear }: {
  lang: Lang; label: string; configured: boolean; pending: boolean; value: string;
  onChange: (v: string) => void; onClear: () => void;
}) {
  const t = T[lang].settings;
  const [show, setShow] = useState(false);
  const [armed, setArmed] = useState(false);
  const timer = useRef<number | undefined>(undefined);
  useEffect(() => () => { if (timer.current) window.clearTimeout(timer.current); }, []);
  const hasInput = value.length > 0;
  const handleClear = () => {
    if (!armed) {
      setArmed(true);
      timer.current = window.setTimeout(() => setArmed(false), 3000);
      return;
    }
    if (timer.current) window.clearTimeout(timer.current);
    setArmed(false);
    onClear();
  };
  const chipClass = pending
    ? "border-[var(--warn)] text-[var(--warn)]"
    : configured
    ? "border-[var(--ok)] text-[var(--ok)]"
    : "border-[var(--muted-foreground)] text-[var(--muted-foreground)]";
  return (
    <div>
      <div className="flex items-center justify-between mb-1">
        <span className={`text-[12px] text-[var(--muted-foreground)] ${lang === "zh" ? "cjk" : "font-mono"}`}>{label}</span>
        <span className={`font-mono text-[10px] tracking-[0.08em] px-1.5 py-0.5 border ${chipClass}`}>
          {pending ? t.tokenPending : configured ? t.configured : t.missing}
        </span>
      </div>
      <div className="flex items-center border-b border-[var(--border)]">
        <input
          type={show ? "text" : "password"}
          value={value}
          onChange={e => onChange(e.target.value)}
          placeholder={configured ? t.tokenPlaceholderSet : t.tokenPlaceholder}
          spellCheck={false}
          autoComplete="off"
          className="flex-1 bg-transparent font-mono text-[11px] text-[var(--foreground)] placeholder-[var(--muted-foreground)] focus:outline-none py-1"
        />
        <button
          onClick={() => setShow(!show)}
          disabled={!hasInput}
          title={hasInput ? undefined : t.showHint}
          className={`font-mono text-[10px] tracking-[0.08em] uppercase px-2 ${hasInput ? "text-[var(--muted-foreground)] hover:text-[var(--foreground)]" : "text-[var(--border)] cursor-not-allowed"}`}>
          {show ? t.hide : t.show}
        </button>
        <button onClick={handleClear}
          className={`font-mono text-[10px] tracking-[0.08em] uppercase px-2 transition-colors
            ${armed ? "text-[var(--danger)] border border-[var(--danger)]" : "text-[var(--muted-foreground)] hover:text-[var(--danger)]"}`}>
          {armed ? `${t.clear} ?` : t.clear}
        </button>
      </div>
    </div>
  );
}

export function SettingsScreen({
  lang, setLang, darkMode, setDarkMode, backendPref, setBackendPref,
  outputDir, setOutputDir, cliPath, setCliPath, env,
  cleanOpts, setCleanOpts, strictVerify, setStrictVerify, onSave,
}: {
  lang: Lang;
  setLang: (l: Lang) => void;
  darkMode: boolean;
  setDarkMode: (v: boolean) => void;
  backendPref: BackendPref;
  setBackendPref: (v: BackendPref) => void;
  outputDir: string;
  setOutputDir: (v: string) => void;
  cliPath: string;
  setCliPath: (v: string) => void;
  env: EnvState | null;
  cleanOpts: CleanOpts;
  setCleanOpts: (v: CleanOpts) => void;
  strictVerify: boolean;
  setStrictVerify: (v: boolean) => void;
  onSave: () => void;
}) {
  const t = T[lang].settings;
  const groups = CLEAN_GROUPS[lang];
  const [mineruToken, setMinerUToken] = useState("");
  const [paddleToken, setPaddleToken] = useState("");
  const [note, setNote] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [copied, setCopied] = useState(false);
  const [savedFlash, setSavedFlash] = useState(false);

  const snapRef = useRef<SettingsSnapshot>({
    backendPref, outputDir, cliPath, cleanOpts: { ...cleanOpts }, strictVerify,
  });
  const snap = snapRef.current;
  const snapshot = (): SettingsSnapshot => ({
    backendPref, outputDir, cliPath, cleanOpts: { ...cleanOpts }, strictVerify,
  });
  // 差异数:把开关拨回去就归零(不再是「操作次数」)
  const dirty =
    (backendPref !== snap.backendPref ? 1 : 0) +
    (outputDir !== snap.outputDir ? 1 : 0) +
    (cliPath !== snap.cliPath ? 1 : 0) +
    (strictVerify !== snap.strictVerify ? 1 : 0) +
    CLEAN_KEYS.reduce((n, k) => n + (cleanOpts[k] !== snap.cleanOpts[k] ? 1 : 0), 0);
  // 凭证是「写入式」字段:已输入但未保存也必须计入未保存,
  // 否则粘完密钥、别处没改动 → 未保存数为 0 → 底部保存栏根本不出现
  const tokenPending = (mineruToken.trim() ? 1 : 0) + (paddleToken.trim() ? 1 : 0);
  const unsaved = dirty + tokenPending;

  const backendIndex = backendPref === "auto" ? 0 : backendPref === "mineru" ? 1 : 2;
  const configuredMineru = env?.mineru_configured ?? false;
  const configuredPaddle = env?.paddle_configured ?? false;

  const discard = () => {
    setBackendPref(snap.backendPref);
    setOutputDir(snap.outputDir);
    setCliPath(snap.cliPath);
    setCleanOpts({ ...snap.cleanOpts });
    setStrictVerify(snap.strictVerify);
    setMinerUToken("");
    setPaddleToken("");
    setNote(null);
    setSavedFlash(false);
  };

  // 凭证:输入框有值才写盘(空 = 不改动;删除用 CLEAR)。返回是否真的写了密钥。
  const saveCredentials = async (): Promise<boolean> => {
    const jobs: { service: string; token: string }[] = [];
    if (mineruToken.trim()) jobs.push({ service: "MinerU", token: mineruToken });
    if (paddleToken.trim()) jobs.push({ service: "PaddleOCR-VL", token: paddleToken });
    if (jobs.length === 0) return false;
    for (const j of jobs) await invoke<string>("save_apikey", { service: j.service, token: j.token });
    setMinerUToken("");
    setPaddleToken("");
    setNote(null);
    return true;
  };

  const clearCredential = async (service: string) => {
    try {
      await invoke<string>("save_apikey", { service, token: "" });
      setNote(null);
      onSave();
    } catch (e) {
      setNote(String(e));
    }
  };

  const handleSave = async () => {
    setSaving(true);
    try {
      const wroteKeys = await saveCredentials();
      snapRef.current = snapshot();   // 保存成功 → 建立新快照(未保存数归零)
      onSave();                        // 落盘设置 + 刷新环境检查(凭证才会变成 CONFIGURED)
      if (wroteKeys) {
        setSavedFlash(true);
        window.setTimeout(() => setSavedFlash(false), 2500);
      }
    } catch (e) {
      setNote(String(e));
    } finally {
      setSaving(false);
    }
  };

  const browseDir = async () => {
    const sel = await open({ directory: true });
    if (sel) setOutputDir(String(sel));
  };

  const copyInstall = async () => {
    try {
      await navigator.clipboard.writeText(t.installCmd);
      setCopied(true);
      setTimeout(() => setCopied(false), 1600);
    } catch { /* 剪贴板不可用时忽略 */ }
  };

  const envRows = [
    { name: t.pandoc, status: env?.pandoc.status ?? "missing", version: env?.pandoc.version ?? null, path: env?.pandoc.path ?? null },
    { name: t.cli, status: env?.engine.status ?? "missing", version: env?.engine.version ?? null, path: env?.engine.path ?? null },
  ];

  return (
    <div className="flex-1 flex flex-col overflow-hidden">
      <div className="gutter pt-6 shrink-0">
        <ChapterHead label={`03 ${T[lang].screens[2]}`} state={unsaved > 0 ? t.dirty(unsaved) : undefined} lang={lang} />
      </div>

      <div className="flex-1 overflow-auto scroll-area gutter pb-24">
        {/* A: 转换器 */}
        <SectionCard lang={lang} letter="A" title={t.sections[0]} summary="">
          <FieldRow lang={lang} label={t.outputDir}>
            <input
              type="text" value={outputDir} onChange={e => setOutputDir(e.target.value)}
              spellCheck={false} autoComplete="off"
              className="flex-1 bg-transparent border-b border-[var(--border)] font-mono text-[11px] text-[var(--foreground)] focus:outline-none focus:border-[var(--primary)] py-1 selectable"
            />
            <button onClick={browseDir} className={`font-mono text-[10px] tracking-[0.08em] uppercase text-[var(--muted-foreground)] hover:text-[var(--foreground)] border border-[var(--border)] px-3 py-1 ${lang === "zh" ? "cjk font-sans" : ""}`}>{t.browse}</button>
          </FieldRow>
          <FieldRow lang={lang} label={t.cliPath}>
            <input
              type="text" value={cliPath} onChange={e => setCliPath(e.target.value)}
              placeholder={t.cliPlaceholder}
              spellCheck={false} autoComplete="off"
              className="flex-1 bg-transparent border-b border-[var(--border)] font-mono text-[11px] text-[var(--foreground)] placeholder-[var(--muted-foreground)] focus:outline-none focus:border-[var(--primary)] py-1 selectable"
            />
          </FieldRow>

          <div className="mt-4">
            <div className="font-mono text-[10px] tracking-[0.12em] uppercase text-[var(--muted-foreground)] mb-2">{t.envCheck}</div>
            <Rule />
            {envRows.map(item => (
              <div key={item.name} className="flex items-center gap-3 py-2 border-b border-[var(--border)]">
                <div className={`w-1.5 h-1.5 shrink-0 ${item.status === "ok" ? "bg-[var(--ok)]" : "bg-[var(--danger)]"}`} />
                <span className="font-mono text-[11px] text-[var(--foreground)] flex-1">{item.name}</span>
                {item.version && <span className="font-mono text-[10px] text-[var(--muted-foreground)] tabular-nums">{item.version}</span>}
                {item.path && <span className="font-mono text-[10px] text-[var(--muted-foreground)] truncate max-w-[280px]">{item.path}</span>}
                <span className={`font-mono text-[10px] tracking-[0.08em] px-1.5 py-0.5 border ${item.status === "ok" ? "border-[var(--ok)] text-[var(--ok)]" : "border-[var(--danger)] text-[var(--danger)]"}`}>
                  {item.status === "ok" ? "OK" : "MISSING"}
                </span>
              </div>
            ))}
            {env && env.pandoc.status !== "ok" && (
              <div className="my-2 border-l-[3px] border-[var(--danger)] pl-4 py-2">
                <p className={`text-[13px] text-[var(--foreground)] mb-2 ${lang === "zh" ? "cjk" : ""}`}>{t.pandocMissing}</p>
                <div className="flex items-center gap-3">
                  <code className="font-mono text-[11px] text-[var(--foreground)] bg-[var(--secondary)] px-3 py-1">{t.installCmd}</code>
                  <button onClick={copyInstall} className="font-mono text-[10px] tracking-[0.08em] border border-[var(--border)] px-2 py-1 text-[var(--muted-foreground)] hover:text-[var(--foreground)]">
                    {copied ? t.copied : t.copy}
                  </button>
                </div>
              </div>
            )}
          </div>
        </SectionCard>
        <Rule />

        {/* B: OCR 与凭证 */}
        <SectionCard lang={lang} letter="B" title={t.sections[1]} summary="">
          <div className={`text-[12px] text-[var(--muted-foreground)] mb-3 ${lang === "zh" ? "cjk" : "font-mono"}`}>{t.backendLabel}</div>
          <div role="radiogroup">
            {t.backends.map(([label, desc], i) => (
              <RadioRow
                key={i}
                label={label}
                desc={desc}
                active={backendIndex === i}
                onClick={() => setBackendPref((i === 0 ? "auto" : i === 1 ? "mineru" : "paddleocr") as BackendPref)}
                lang={lang}
              />
            ))}
          </div>
          <div className="mt-4 space-y-3">
            <TokenRow
              lang={lang}
              label={t.mineruToken}
              configured={configuredMineru}
              pending={mineruToken.trim().length > 0}
              value={mineruToken}
              onChange={setMinerUToken}
              onClear={() => void clearCredential("MinerU")}
            />
            <TokenRow
              lang={lang}
              label={t.paddleToken}
              configured={configuredPaddle}
              pending={paddleToken.trim().length > 0}
              value={paddleToken}
              onChange={setPaddleToken}
              onClear={() => void clearCredential("PaddleOCR-VL")}
            />
            {savedFlash && (
              <p className={`text-[10px] text-[var(--ok)] ${lang === "zh" ? "cjk" : "font-mono"}`}>{t.tokenSaved}</p>
            )}
            {note && <p className={`text-[10px] leading-relaxed text-[var(--danger)] break-all ${lang === "zh" ? "cjk" : "font-mono"}`}>{note}</p>}
          </div>
        </SectionCard>
        <Rule />

        {/* C: 校勘流水线 */}
        <SectionCard lang={lang} letter="C" title={t.sections[2]}
          summary={`${CLEAN_KEYS.filter(k => cleanOpts[k]).length}/${CLEAN_KEYS.length} ON`}>
          {groups.map((group, gi) => {
            const onInGroup = group.items.filter(it => cleanOpts[it.key as CleanKey]).length;
            return (
              <div key={gi} className="mb-4">
                {/* 分组标题只做分区说明:批量开合交给每一项自己的开关(九项各自独立决策,
                    一个总开关很容易把用户刚调好的项全部冲掉) */}
                <div className="flex items-center gap-4 py-1.5 border-b border-[var(--border)]">
                  <span className={`text-[12px] text-[var(--muted-foreground)] flex-1 ${lang === "zh" ? "cjk font-medium" : "font-mono tracking-[0.08em]"}`}>{group.name}</span>
                  <span className="font-mono text-[10px] text-[var(--muted-foreground)] tabular-nums">
                    {onInGroup}/{group.items.length}
                  </span>
                </div>
                {group.items.map(item => {
                  const key = item.key as CleanKey;
                  const on = cleanOpts[key];
                  const toggle = () => setCleanOpts({ ...cleanOpts, [key]: !on });
                  // 行只响应鼠标;键盘入口是行内那个开关本身 —— 否则 Tab 会停在
                  // 一个不可见的 div 上,空格同时触发行与按钮,切两次等于没切。
                  return (
                    <div key={item.key} title={item.key}
                      className="group flex items-center h-11 gap-4 border-b border-[var(--border)] hover:bg-[var(--secondary)] transition-colors cursor-pointer"
                      onClick={toggle}>
                      <span className={`font-mono text-[13px] w-5 shrink-0 text-center transition-colors ${on ? "text-[var(--primary)]" : "text-[var(--muted-foreground)]"}`}>{item.glyph}</span>
                      <div className="flex-1 min-w-0">
                        <div className={`text-[13px] ${on ? "text-[var(--foreground)]" : "text-[var(--muted-foreground)]"} transition-colors ${lang === "zh" ? "cjk" : ""}`}>
                          {item.label}
                          {item.advanced && (
                            <span className="ml-2 font-mono text-[10px] tracking-[0.08em] border border-[var(--muted-foreground)] text-[var(--muted-foreground)] px-1">ADVANCED</span>
                          )}
                        </div>
                        <div className={`text-[12px] text-[var(--muted-foreground)] ${lang === "zh" ? "cjk" : "font-mono"}`}>{item.desc}</div>
                      </div>
                      <ToggleSwitch checked={on} onChange={toggle} label={item.label} />
                    </div>
                  );
                })}
              </div>
            );
          })}
          <p className={`text-[10px] text-[var(--muted-foreground)] ${lang === "zh" ? "cjk" : "font-mono"}`}>{t.cleanNote}</p>
        </SectionCard>
        <Rule />

        {/* D: 质量与外观 */}
        <SectionCard lang={lang} letter="D" title={t.sections[3]} summary="">
          <div className="border-b border-[var(--border)]">
            <Switch
              checked={strictVerify}
              onChange={setStrictVerify}
              label={t.epubValidation}
              description={t.epubValidationDesc}
              lang={lang}
            />
          </div>
          <div className="py-3 border-b border-[var(--border)]">
            <div className={`text-[13px] text-[var(--muted-foreground)] mb-2 ${lang === "zh" ? "cjk" : ""}`}>{t.themeLabel}</div>
            <Segment
              options={[{ value: "light", label: t.themeLight }, { value: "dark", label: t.themeDark }]}
              value={darkMode ? "dark" : "light"}
              onChange={v => setDarkMode(v === "dark")}
            />
          </div>
          <div className="py-3 border-b border-[var(--border)]">
            <div className={`text-[13px] text-[var(--muted-foreground)] mb-2 ${lang === "zh" ? "cjk" : ""}`}>{t.langLabel}</div>
            <Segment
              options={[{ value: "en", label: t.langEn }, { value: "zh", label: t.langZh }]}
              value={lang}
              onChange={v => setLang(v as Lang)}
            />
          </div>
        </SectionCard>
      </div>

      {unsaved > 0 && (
        <div className="shrink-0 border-t border-[var(--border)] flex items-center justify-end gap-4 gutter py-3 bg-[var(--card)]">
          <span className="font-mono text-[11px] text-[var(--muted-foreground)] flex-1">{t.dirty(unsaved)}</span>
          <button onClick={discard} className={`font-mono text-[10px] tracking-[0.08em] uppercase text-[var(--muted-foreground)] hover:text-[var(--foreground)] ${lang === "zh" ? "cjk font-sans" : ""}`}>{t.discard}</button>
          <button onClick={() => void handleSave()} disabled={saving} className={`px-6 py-2 bg-[var(--primary)] text-[var(--primary-foreground)] font-mono text-[10px] tracking-[0.1em] uppercase hover:opacity-90 transition-opacity disabled:opacity-60 ${lang === "zh" ? "cjk font-sans" : ""}`}>{t.save}</button>
        </div>
      )}
    </div>
  );
}
