"""验证大模型配置界面的结构、交互与无 JS 错误。

沿用 verify_timeline.py 的做法：Edge 无头加载 + 捕获 JS 异常 +
把结果写进 document.title 再 dump-dom 取回。
这类"id 拼错、函数未定义、时序问题"的 bug 只有真跑一遍才抓得住。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"

ERROR_HOOK = """
<script>
window.__errs = [];
window.addEventListener('error', (e) => {
  window.__errs.push('ERROR: ' + (e.message||'') + ' @' + (e.lineno||0));
});
window.addEventListener('unhandledrejection', (e) => {
  window.__errs.push('PROMISE: ' + (e.reason && e.reason.message ? e.reason.message : String(e.reason)));
});
const _ce = console.error;
console.error = function(){ window.__errs.push('CONSOLE: ' + Array.from(arguments).join(' ')); _ce.apply(console, arguments); };
</script>
"""

CHECKS = """
<script>
setTimeout(async () => {
  const R = {};
  try {
    // 元素齐全
    ['sProvider','providerNote','sApiKey','btnToggleKey','btnGetKey','sLlmModel',
     'sApiBase','btnTestLlm','llmStatus','envNote','rowApi','rowApiKey','rowApiModel',
     'rowApiBase','rowApiTest','rowApiEnv'].forEach(id => {
      R['el_' + id] = !!document.getElementById(id);
    });
    // 函数存在
    ['loadProviders','syncProviderFields','testLlm','collectSettings','applySettings']
      .forEach(fn => { R['fn_' + fn] = typeof window[fn]; });

    // 服务商下拉是否被填充
    const sel = document.getElementById('sProvider');
    R.option_count = sel ? sel.options.length : 0;
    R.first_option = sel && sel.options.length ? sel.options[0].text : '';

    // 触发"翻译"开关以显示配置区
    const trBox = document.getElementById('sTr');
    if (trBox) { trBox.checked = true; trBox.dispatchEvent(new Event('change', {bubbles:true})); }
    await new Promise(r => setTimeout(r, 200));

    // 选 deepseek 时应自动填端点和模型
    if (sel) {
      sel.value = 'deepseek';
      sel.dispatchEvent(new Event('change', {bubbles:true}));
      await new Promise(r => setTimeout(r, 120));
    }
    R.base_val = document.getElementById('sApiBase').value;
    R.model_val = document.getElementById('sLlmModel').value;
    R.note_val = document.getElementById('providerNote').textContent;
    R.row_visible = document.getElementById('rowApi').style.display !== 'none';

    // 切到自定义，应清空 placeholders
    if (sel) {
      sel.value = 'custom';
      sel.dispatchEvent(new Event('change', {bubbles:true}));
      await new Promise(r => setTimeout(r, 120));
    }
    R.custom_note = document.getElementById('providerNote').textContent;
    R.custom_getkey_hidden = document.getElementById('btnGetKey').style.display === 'none';

    // 收集设置应带上三个新字段
    const st = window.collectSettings();
    R.has_provider = 'provider' in st;
    R.has_base = 'api_base' in st;
    R.has_model = 'llm_model' in st;

    // 测试连接的 mock 路径
    await window.testLlm();
    R.test_status = document.getElementById('llmStatus').textContent;

    R.errors = window.__errs || [];
  } catch(e) {
    R.fatal = e.message + ' | ' + (e.stack||'').split('\\n')[1];
    R.errors = window.__errs || [];
  }
  document.title = 'RESULT:' + JSON.stringify(R);
}, 2200);
</script>
"""

MOCK = """
<script>
window.pywebview = { api: {
  get_env: async () => ({ffmpeg:true, device:'cuda', gpu_name:'NVIDIA GeForce GTX 1080 Ti', version:'1.1.0'}),
  load_settings: async () => ({model:'turbo', language:'zh', formats:['srt'], compute_type:'auto', device:'auto'}),
  save_settings: async () => ({ok:true}),
  list_llm_providers: async () => ({
    ok: true,
    providers: [
      {id:'deepseek', name:'DeepSeek（深度求索）', base:'https://api.deepseek.com/v1',
       model:'deepseek-chat', key_url:'https://platform.deepseek.com/api_keys', note:'便宜、中文好，翻译首选'},
      {id:'openai', name:'OpenAI', base:'https://api.openai.com/v1',
       model:'gpt-4o-mini', key_url:'https://platform.openai.com/api-keys', note:'需要能访问外网'},
      {id:'dashscope', name:'阿里云百炼（通义千问）',
       base:'https://dashscope.aliyuncs.com/compatible-mode/v1', model:'qwen-plus',
       key_url:'https://bailian.console.aliyun.com/', note:'国内直连，有免费额度'},
      {id:'ollama', name:'本地 Ollama', base:'http://127.0.0.1:11434/v1',
       model:'qwen2.5:7b', key_url:'https://ollama.com/download', note:'完全本地，不需要 Key'},
      {id:'custom', name:'自定义 / 中转', base:'', model:'', key_url:'', note:'任何 OpenAI 兼容接口'},
    ],
    current: {provider:'deepseek', base:'https://api.deepseek.com/v1', has_key:false, key_source:''},
  }),
  test_llm: async () => ({ok:true, base:'https://api.deepseek.com/v1',
                          model:'deepseek-chat', used_model:'deepseek-chat', elapsed:0.62}),
  open_url: async () => ({ok:true}),
  probe: async (ps) => [], list_media: async () => [], pick_files: async () => [],
  pick_folder: async () => '', reveal: async () => ({ok:true}),
  resolve_dropped: async () => ({paths:[], need_dialog:true}),
  start_job: async () => ({ok:true}), cancel_job: async () => ({ok:true}),
  poll_events: async () => [],
}};
setTimeout(() => window.dispatchEvent(new Event('pywebviewready')), 40);
</script>
"""


def main() -> int:
    html = open(os.path.join(APP, "webui", "index.html"), encoding="utf-8").read()
    page = os.path.join(APP, "out", "_llmtest.html")
    os.makedirs(os.path.dirname(page), exist_ok=True)
    with open(page, "w", encoding="utf-8") as f:
        f.write(html.replace("</head>", ERROR_HOOK + "</head>")
                    .replace("</body>", MOCK + CHECKS + "</body>"))

    p = page.replace("\\", "/")
    r = subprocess.run(
        [EDGE, "--headless=new", "--disable-gpu", "--virtual-time-budget=7000",
         "--dump-dom", f"file:///{p}"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180,
    )
    m = re.search(r"<title>RESULT:(.*?)</title>", r.stdout or "", re.S)
    if not m:
        print("未能取到结果")
        print((r.stdout or "")[:500])
        return 1

    d = json.loads(m.group(1))
    fails = []

    def chk(name, cond, detail=""):
        print(f"  {'PASS' if cond else 'FAIL'}  {name}" + ("" if cond else f"   {detail}"))
        if not cond:
            fails.append(name)

    print("=" * 62)
    print("大模型配置界面 · 验证")
    print("=" * 62)

    print("\n[1] 无 JS 错误")
    errs = [e for e in (d.get("errors") or []) if "ResizeObserver" not in e]
    chk("无运行时异常", not errs, str(errs[:3]))
    chk("无致命错误", "fatal" not in d, str(d.get("fatal", "")))

    print("\n[2] 元素与函数")
    bad_el = [k[3:] for k, v in d.items() if k.startswith("el_") and not v]
    chk("配置区元素齐全", not bad_el, str(bad_el))
    bad_fn = [k[3:] for k, v in d.items()
              if k.startswith("fn_") and v not in ("function",)]
    chk("核心函数已定义", not bad_fn, str(bad_fn))

    print("\n[3] 服务商下拉")
    chk("选项已填充", d.get("option_count", 0) >= 5, f"count={d.get('option_count')}")
    print(f"       首项: {d.get('first_option')}")

    print("\n[4] 联动填值")
    chk("选 deepseek 自动填端点",
        (d.get("base_val") or "").startswith("https://api.deepseek.com"),
        str(d.get("base_val")))
    chk("选 deepseek 自动填模型", bool(d.get("model_val")), str(d.get("model_val")))
    chk("显示服务商说明", bool(d.get("note_val")), str(d.get("note_val")))
    chk("配置区已展开", d.get("row_visible") is True, str(d.get("row_visible")))
    print(f"       端点={d.get('base_val')}  模型={d.get('model_val')}")
    print(f"       说明={d.get('note_val')}")

    print("\n[5] 自定义模式")
    chk("自定义有提示文字", bool(d.get("custom_note")), str(d.get("custom_note")))
    chk("自定义隐藏「去申请」", d.get("custom_getkey_hidden") is True,
        str(d.get("custom_getkey_hidden")))

    print("\n[6] 设置收集")
    chk("含 provider 字段", d.get("has_provider") is True)
    chk("含 api_base 字段", d.get("has_base") is True)
    chk("含 llm_model 字段", d.get("has_model") is True)

    print("\n[7] 测试连接")
    print(f"       状态: {d.get('test_status')}")

    print("\n" + "=" * 62)
    if fails:
        print(f"失败 {len(fails)} 项：{', '.join(fails)}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
