/* DataForge — motor Python rodando no navegador (Web Worker + Pyodide).
   As planilhas são processadas aqui, no computador de quem usa: nada é enviado a servidores. */
const PYODIDE_VERSION = "0.27.2";
let BASE = `https://cdn.jsdelivr.net/pyodide/v${PYODIDE_VERSION}/full/`;
const PY_FILES = ["__init__.py", "nlp.py", "config.py", "profiler.py", "operations.py",
                  "storage.py", "exporter.py", "agents.py", "api.py"];
const MUTATING = new Set(["/api/upload", "/api/rename", "/api/remove", "/api/execute", "/api/settings",
                          "/api/feedback", "/api/forget", "/api/plan"]);
let py = null;
let readyPromise = null;

const progress = (msg) => self.postMessage({ type: "progress", msg });
const syncfs = (populate) => new Promise((ok) => py.FS.syncfs(populate, () => ok()));

async function boot() {
  progress("Baixando o motor de dados…");
  importScripts(BASE + "pyodide.js");
  py = await loadPyodide({ indexURL: BASE });
  progress("Carregando bibliotecas (pandas)…");
  await py.loadPackage(["pandas", "sqlite3", "micropip", "xlrd"]);
  const micropip = py.pyimport("micropip");
  await micropip.install(["openpyxl"]);

  progress("Preparando armazenamento local…");
  py.FS.mkdirTree("/data");
  py.FS.mount(py.FS.filesystems.IDBFS, {}, "/data");
  await syncfs(true);

  progress("Carregando agentes…");
  py.FS.mkdirTree("/app/dataforge");
  const ver = Date.now();
  for (const f of PY_FILES) {
    const r = await fetch(new URL(`dataforge/${f}?v=${ver}`, self.location));
    if (!r.ok) throw new Error(`não foi possível carregar ${f}`);
    py.FS.writeFile(`/app/dataforge/${f}`, await r.text());
  }
  py.runPython(`
import os, sys, json
os.environ["DATAFORGE_DIR"] = "/data"
sys.path.insert(0, "/app")
import js
from dataforge import api, agents

def _xhr_post(url, headers, body, timeout):
    x = js.XMLHttpRequest.new()
    x.open("POST", url, False)
    for k, v in headers.items():
        x.setRequestHeader(k, v)
    try:
        x.send(body)
    except Exception as e:
        return 0, f"sem conexão com a IA: {e}"
    return int(x.status), str(x.responseText)

agents.HTTP_POST = _xhr_post

def _handle(req):
    r = json.loads(req)
    try:
        code, payload = api.dispatch(r["method"], r["path"], r.get("body"))
    except Exception as e:
        import traceback; traceback.print_exc()
        code, payload = 500, {"erro": f"{type(e).__name__}: {e}"}
    return json.dumps([code, payload], ensure_ascii=False, default=str)
`);
  py.runPython(`api.dispatch("GET", "/api/settings")`);
  await syncfs(false);
  progress("Pronto");
}

self.onmessage = async (e) => {
  const { id, type } = e.data;
  if (type === "init") {
    if (e.data.base) BASE = e.data.base;
    readyPromise = readyPromise || boot();
    try { await readyPromise; self.postMessage({ id, ok: true }); }
    catch (err) { self.postMessage({ id, error: String(err && err.message || err) }); }
    return;
  }
  try {
    await readyPromise;
    const { method, path, body } = e.data;
    py.globals.set("_req", JSON.stringify({ method, path, body }));
    const [code, payload] = JSON.parse(py.runPython("_handle(_req)"));
    if (MUTATING.has(path)) await syncfs(false);
    self.postMessage({ id, code, payload });
  } catch (err) {
    self.postMessage({ id, code: 500, payload: { erro: String(err && err.message || err) } });
  }
};
