/**
 * 面板前端的沙箱测试。
 *
 * 为什么需要它：面板跑在不透明源的 iframe 里，自己发 fetch 会被判跨域失败，
 * 只能走宿主注入的 postMessage 桥。但桥接受的端点前缀在不同 AstrBot 版本里不一致，
 * 所以页面里做了"逐个候选探测"。这段逻辑没法用 Python 测，只能用 Node 把它跑起来。
 *
 *     node tests/panel_check.mjs
 */

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import vm from "node:vm";

const HERE = dirname(fileURLToPath(import.meta.url));
const HTML = readFileSync(join(HERE, "..", "pages", "mind", "index.html"), "utf8");
const BLOCKS = [...HTML.matchAll(/<script(?![^>]*src=)[^>]*>([\s\S]*?)<\/script>/g)].map((m) => m[1]);
if (BLOCKS.length === 0) throw new Error("页面里一个内联 script 都没有");
// 页面里除了主逻辑，还可能有一小段引导脚本（例如「尽早贴上主题、避免白闪」——
// 那段必须在 <head> 里同步跑，放到文末就来不及了）。
// 真正要分析的是最长的那段，也就是主逻辑。
const SCRIPT = BLOCKS.reduce((a, b) => (b.length > a.length ? b : a));

// 静态检查：JS 的函数声明是"后者覆盖前者"，
// 一旦文件里出现重复定义（例如补丁把一段插了两次），跑起来的可能根本不是新代码。
// 这个坑真踩过一次，所以固化成检查项。
{
  const names = [...SCRIPT.matchAll(/^  function (\w+)\(/gm)].map((m) => m[1]);
  const dup = [...new Set(names.filter((n, i) => names.indexOf(n) !== i))];
  if (dup.length) {
    console.log("❌ 面板脚本里有重复的函数定义：" + dup.join(", "));
    process.exit(1);
  }
  console.log("✅ 面板脚本无重复函数定义（共 " + names.length + " 个函数）");
}

// ---- 静态检查：id 不能重复 ----
// 真踩过：重建页面区域时把旧的保存卡片留下了，页面上出现两个 id="pr-save"，
// 于是 $("pr-save") 拿到的是先出现的那个 —— 按钮点了没反应，还很难查。
{
  const ids = [...HTML.matchAll(/\sid="([^"]+)"/g)].map((m) => m[1]);
  const dup = [...new Set(ids.filter((id, i) => ids.indexOf(id) !== i))];
  if (dup.length) {
    console.log("❌ 面板里有重复的 id：" + dup.join(", "));
    process.exit(1);
  }
  console.log("✅ 面板 id 无重复（共 " + ids.length + " 个）");
}

// ---- 结构检查：视图不能被套在另一个视图里 ----
// 真踩过：新加的「分段拟人」视图插在了「人格定制」的收尾 </div> 之前，
// 于是它成了 view-persona 的子节点 —— 点页签时自己的 display 是打开了，
// 但父节点是 display:none，整页看上去就是一片空白。
// 标签配平检查抓不到这种错，必须单独卡一道。
{
  const cleaned = HTML
    .replace(/<script[\s\S]*?<\/script>/gi, "")
    .replace(/<style[\s\S]*?<\/style>/gi, "")
    .replace(/<!--[\s\S]*?-->/g, "");
  const VOID = new Set(["area", "base", "br", "col", "embed", "hr", "img", "input",
                        "link", "meta", "param", "source", "track", "wbr"]);
  const re = /<(\/?)([a-zA-Z][a-zA-Z0-9-]*)([^>]*?)(\/?)>/g;
  const stack = [];
  const problems = [];
  const views = [];
  let match;
  while ((match = re.exec(cleaned)) !== null) {
    const closing = match[1] === "/";
    const tag = match[2].toLowerCase();
    if (VOID.has(tag) || match[4] === "/") continue;
    if (!closing) {
      const found = /\sid="(view-[a-z]+)"/.exec(match[3]);
      const view = found ? found[1] : "";
      if (view) {
        views.push(view);
        // 正确的位置是 <body><div class="wrap"><div id="view-*">，也就是栈里正好三层
        if (stack.length !== 3) {
          problems.push(view + " 被套在别的元素里（当前打开的标签：" + stack.join(" > ") + "）");
        }
      }
      stack.push(tag);
      continue;
    }
    stack.pop();
  }
  if (problems.length) {
    console.log("❌ 面板视图被套住了，点页签会显示空白：");
    problems.forEach((p) => console.log("   " + p));
    process.exit(1);
  }
  console.log("✅ 面板视图都是 .wrap 的直接子节点（" + views.length + " 个：" + views.join(" ") + "）");
}

// ---- 静态检查：不许用会被沙箱拦掉的模态框 ----
// 沙箱 iframe 默认没有 allow-modals，window.confirm() 会被浏览器直接拦掉并返回 false，
// 代码就停在 if (!confirm(...)) return; 那一行 —— 表现为"点了没反应也不报错"。
// 面板里全部破坏性操作都踩过这个坑，所以固化成一票否决。
{
  const modalCalls = [...SCRIPT.matchAll(/window\.(confirm|alert|prompt)\s*\(/g)];
  if (modalCalls.length) {
    console.log("❌ 面板脚本里还有 " + modalCalls.length + " 处 window.confirm/alert/prompt");
    console.log("   沙箱 iframe 会静默拦掉它们，请改用 needConfirm() 两步确认");
    process.exit(1);
  }
  console.log("✅ 面板脚本没有用会被沙箱拦截的模态框");
}

// ---- 结构检查：标签必须配平 ----
// 真踩过：插入新按钮时多留了一个没闭合的 <div class="row">，
// .row 是 flex 容器，后面所有按钮行被并进同一行挤成一团，看起来像"按钮消失了"。
// Python 侧完全看不见这种问题，所以在这里卡住。
function checkHtmlBalance(html) {
  const cleaned = html
    .replace(/<script[\s\S]*?<\/script>/gi, "")
    .replace(/<style[\s\S]*?<\/style>/gi, "")
    .replace(/<!--[\s\S]*?-->/g, "");
  const VOID = new Set(["area", "base", "br", "col", "embed", "hr", "img", "input",
                        "link", "meta", "param", "source", "track", "wbr"]);
  const stack = [];
  const problems = [];
  const re = /<(\/?)([a-zA-Z][a-zA-Z0-9-]*)([^>]*?)(\/?)>/g;
  let match;
  while ((match = re.exec(cleaned)) !== null) {
    const closing = match[1] === "/";
    const tag = match[2].toLowerCase();
    if (VOID.has(tag) || match[4] === "/") continue;
    if (!closing) {
      stack.push(tag);
      continue;
    }
    const top = stack.pop();
    if (top !== tag) {
      problems.push("闭合标签 </" + tag + "> 对不上，此时栈顶是 " + (top || "（空）"));
      break;
    }
  }
  if (!problems.length && stack.length) {
    problems.push("有 " + stack.length + " 个标签没闭合：" + stack.join(" > "));
  }
  return problems;
}

{
  const problems = checkHtmlBalance(HTML);
  if (problems.length) {
    console.log("❌ 面板 HTML 结构有问题：");
    problems.forEach((p) => console.log("   " + p));
    process.exit(1);
  }
  console.log("✅ 面板 HTML 标签配平");
}

const PLUGIN = "astrbot_plugin_ai_mind";
//: 插件实际注册的子路径（和后端 _route_defs 保持一致）
const KNOWN_ROUTES = [
  PLUGIN + "/sessions", PLUGIN + "/emotion", PLUGIN + "/emotion/set",
  PLUGIN + "/emotion/point", PLUGIN + "/emotion/reset", PLUGIN + "/emotion/curve/clear",
  PLUGIN + "/emotion/freeze", PLUGIN + "/memory", PLUGIN + "/memory/add",
  PLUGIN + "/memory/update", PLUGIN + "/memory/delete", PLUGIN + "/memory/batch",
  PLUGIN + "/images", PLUGIN + "/images/upload", PLUGIN + "/images/trigger/add",
  PLUGIN + "/images/trigger/update", PLUGIN + "/images/trigger/delete",
  PLUGIN + "/images/delete", PLUGIN + "/images/batch", PLUGIN + "/_echo",
  PLUGIN + "/debounce", PLUGIN + "/debounce/preview", PLUGIN + "/debounce/save",
  PLUGIN + "/splitter", PLUGIN + "/splitter/preview", PLUGIN + "/splitter/save",
  PLUGIN + "/splitter/reset", PLUGIN + "/relationship", PLUGIN + "/relationship/save",
  PLUGIN + "/relationship/reset", PLUGIN + "/relationship/relation",
  PLUGIN + "/theory", PLUGIN + "/theory/clear",
  PLUGIN + "/settings", PLUGIN + "/settings/save", PLUGIN + "/manage",
  PLUGIN + "/manage/memory", PLUGIN + "/manage/data",
  PLUGIN + "/wizard", PLUGIN + "/wizard/apply", PLUGIN + "/wizard/mode"
];
const SESSIONS_OK = { sessions: [{ key: "s1", label: "私聊 · 1", group: false, samples: 3 }], stats: {} };

function makeCanvasCtx() {
  const noop = () => {};
  return new Proxy(
    { measureText: () => ({ width: 10 }), canvas: {} },
    { get: (t, k) => (k in t ? t[k] : noop), set: () => true }
  );
}

function makeElement(id) {
  return {
    id, value: "", textContent: "", innerHTML: "", className: "", disabled: false,
    dataset: {}, style: {},
    classList: { add() {}, remove() {}, contains: () => false },
    appendChild() {}, querySelector: () => null, querySelectorAll: () => [],
    addEventListener() {}, closest: () => null,
    getContext: () => makeCanvasCtx(),
    clientWidth: 800, clientHeight: 300, width: 0, height: 0,
    getBoundingClientRect: () => ({ left: 0, top: 0, width: 800, height: 300 })
  };
}

/** 跑一次页面脚本，返回它的可观测行为。 */
async function run({ bridge, fetchImpl, origin = "http://astrbot.local" }) {
  const elements = new Map();
  const fetched = [];
  const bridgeCalls = [];

  const sandbox = {
    console,
    setTimeout: (fn) => setTimeout(fn, 0),
    clearTimeout: (t) => clearTimeout(t),
    Promise, JSON, Math, Date, Number, String, Object, Array, Error, RegExp,
    encodeURIComponent, decodeURIComponent,
    devicePixelRatio: 1
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  sandbox.location = { origin };
  sandbox.document = {
    getElementById(id) {
      if (!elements.has(id)) elements.set(id, makeElement(id));
      return elements.get(id);
    },
    createElement: (tag) => makeElement(tag),
    querySelector: () => null,
    querySelectorAll: () => [],
    addEventListener() {},
    documentElement: { getAttribute: () => "light", setAttribute() {}, style: {} }
  };
  sandbox.getComputedStyle = () => ({ getPropertyValue: () => "#888888" });
  sandbox.addEventListener = () => {};

  if (bridge) {
    sandbox.AstrBotPluginPage = {
      ready: () => Promise.resolve({}),
      getContext: () => ({}),
      getLocale: () => "zh-CN",
      t: (k, f) => f,
      onContext: () => () => {},
      apiGet(endpoint, params) {
        bridgeCalls.push({ method: "GET", endpoint, params });
        // 复现真实宿主的校验：endpoint 里带 ? 一律直接拒掉
        if (String(endpoint).indexOf("?") >= 0) {
          return Promise.reject(new Error("Plugin bridge endpoint is invalid."));
        }
        const verdict = bridge(endpoint);
        if (verdict === true) {
          return Promise.resolve(endpoint.indexOf("sessions") >= 0 ? SESSIONS_OK : { current: null });
        }
        return Promise.reject(new Error(verdict || "bridge rejected"));
      },
      apiPost(endpoint, body) {
        bridgeCalls.push({ method: "POST", endpoint, body });
        if (String(endpoint).indexOf("?") >= 0) {
          return Promise.reject(new Error("Plugin bridge endpoint is invalid."));
        }
        const verdict = bridge(endpoint);
        return verdict === true ? Promise.resolve({ ok: true }) : Promise.reject(new Error(verdict || "bridge rejected"));
      }
    };
  }

  sandbox.fetch = (url) => {
    fetched.push(String(url));
    const verdict = fetchImpl ? fetchImpl(String(url)) : "no fetch allowed";
    if (verdict === true) {
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve(String(url).indexOf("sessions") >= 0 ? SESSIONS_OK : { current: null })
      });
    }
    return Promise.reject(new Error(verdict));
  };

  const context = vm.createContext(sandbox);
  vm.runInContext(SCRIPT, context, { filename: "panel.js" });
  await new Promise((r) => setTimeout(r, 30));

  const status = elements.get("api-status");
  return {
    status: status ? status.textContent : "",
    statusBad: status ? status.className.indexOf("err") >= 0 : false,
    fetched,
    bridgeCalls,
    toast: elements.get("toast") ? elements.get("toast").textContent : ""
  };
}

const cases = [
  {
    name: "查询参数走 params，绝不拼进 endpoint（宿主会校验 endpoint）",
    opts: {
      bridge: (ep) => ep.indexOf(PLUGIN + "/") === 0,
      // 让 sessions 返回带 session 的数据，好触发后续带参数的 emotion 请求
      sessions: SESSIONS_OK
    },
    check: (r) => {
      const withQuery = r.bridgeCalls.filter((c) => String(c.endpoint).indexOf("?") >= 0);
      if (withQuery.length) return false;
      const emotionCall = r.bridgeCalls.find((c) => c.endpoint.indexOf("emotion") >= 0);
      if (!emotionCall) return false;
      // 参数必须以对象形式传进去
      return !!(emotionCall.params && emotionCall.params.hours);
    }
  },
  {
    name: "实测场景：宿主自己补 /api/plug/，只接受插件相对路径（v4.28.1 的行为）",
    opts: {
      bridge: (ep) => {
        // 忠实模拟 v4.28.1 的宿主 + 后端：
        //   宿主把端点拼成 /api/plug/<插件名>/<ep>
        //   后端取出 <插件名>/<ep> 拿去和"已注册路由"比对
        // 已注册路由是 /<插件名>/<子路径>，所以只有**纯子路径**能匹配上；
        // 再多带一层插件名就会变成 .../插件名/插件名/... 而落进兜底。
        const hostPath = "/api/plug/" + PLUGIN + "/" + ep;
        const pluginPath = hostPath.slice("/api/plug/".length);
        return KNOWN_ROUTES.indexOf(pluginPath) >= 0 ? true : "未找到该路由";
      }
    },
    check: (r) =>
      r.status.includes("bridge") &&
      !r.statusBad &&
      !r.toast &&
      r.bridgeCalls.length > 0 &&
      // 面板启动时会先问一次面板模式，所以不能死认第一条；只要 sessions 走的是
      // 插件相对路径（不带查询串）就算通过
      r.bridgeCalls.some((call) => String(call.endpoint).indexOf("?") < 0 &&
        String(call.endpoint).endsWith("sessions"))
  },
  {
    name: "宿主原样发出：/api/plug/ 绝对路径也能用",
    opts: { bridge: (ep) => ep.indexOf("/api/plug/" + PLUGIN + "/") === 0 },
    check: (r) => r.status.includes("/api/plug/") && !r.statusBad
  },
  {
    name: "桥只认绝对 /api/plug/ 时也能命中",
    opts: { bridge: (ep) => ep.indexOf("/api/plug/" + PLUGIN + "/") === 0 },
    check: (r) => r.status.includes("bridge") && r.status.includes("/api/plug/") && !r.statusBad && !r.toast
  },
  {
    name: "桥只认 /plug/（父窗口自己补 /api）—— 应当跳过第一个候选",
    opts: { bridge: (ep) => ep.indexOf("/plug/" + PLUGIN + "/") === 0 },
    check: (r) => r.status.includes("/plug/") && !r.status.includes("/api/plug/") && !r.statusBad && !r.toast
  },
  {
    name: "桥只认 /plugins/extensions/（新版本路由）",
    opts: { bridge: (ep) => ep.indexOf("/plugins/extensions/" + PLUGIN + "/") === 0 },
    check: (r) => r.status.includes("/plugins/extensions/") && !r.statusBad && !r.toast
  },
  {
    name: "没有桥、同源 fetch 可用 —— 应当自动回退到 fetch",
    opts: { bridge: null, fetchImpl: (u) => u.indexOf("/api/plug/" + PLUGIN + "/") >= 0 },
    check: (r) => r.status.includes("fetch") && !r.statusBad && !r.toast
  },
  {
    name: "桥和 fetch 全不通 —— 必须给出可诊断的失败信息而不是静默",
    opts: { bridge: () => "接口不存在", fetchImpl: () => "网络错误" },
    check: (r) =>
      r.statusBad &&
      r.status.includes("接口不通") &&
      r.status.includes("接口不存在") &&
      r.status.includes("种组合都失败") &&
      r.toast.includes("加载失败") &&
      r.toast.length < 120
  },
  {
    name: "不透明源（origin=null）时不该尝试 fetch",
    opts: {
      origin: "null",
      bridge: (ep) => ep.indexOf("/api/plug/" + PLUGIN + "/") === 0
    },
    check: (r) => r.fetched.length === 0 && !r.statusBad
  }
];

let failed = 0;
for (const item of cases) {
  let result;
  try {
    result = await run(item.opts);
  } catch (err) {
    console.log("❌ " + item.name + " —— 脚本抛异常：" + err.message);
    failed += 1;
    continue;
  }
  const ok = item.check(result);
  console.log((ok ? "✅ " : "❌ ") + item.name);
  if (!ok) {
    failed += 1;
    console.log("     status = " + JSON.stringify(result.status));
    console.log("     toast  = " + JSON.stringify(result.toast));
    console.log("     bridge = " + JSON.stringify(result.bridgeCalls.map((c) => c.endpoint)));
    console.log("     fetch  = " + JSON.stringify(result.fetched));
  }
}

console.log("");
console.log(failed === 0 ? "面板传输层：" + cases.length + " 项全部通过" : "面板传输层：失败 " + failed + " 项");
process.exit(failed === 0 ? 0 : 1);

