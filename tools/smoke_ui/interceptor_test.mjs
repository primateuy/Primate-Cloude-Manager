/**
 * Test unitario del interceptor de acciones `handlePcmActionResult` (pcm_actions.js).
 *
 * Ejercita el caso "wizard que encadena otra acción al confirmar" SIN navegador:
 * ningún wizard del módulo PCM encadena acciones hoy, así que se simula pasando
 * al interceptor cada forma de acción devuelta y verificando a qué canal la rutea.
 *
 * Corre con: node tools/smoke_ui/interceptor_test.mjs
 */
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const srcPath = path.join(
    here, "..", "..",
    "primate_cloud_manager/static/src/app/pcm_actions.js"
);

// Importar el CÓDIGO REAL: se lee el archivo, se le quitan los `export` (para
// poder importarlo como data-URL ESM) y se re-exporta lo que se testea.
const raw = fs.readFileSync(srcPath, "utf8").replace(/^export /gm, "");
const mod = await import(
    "data:text/javascript," +
    encodeURIComponent(raw + "\nexport { handlePcmActionResult };")
);
const { handlePcmActionResult } = mod;

// env.pcm mock que registra a qué canal llegó cada acción.
function makeEnv() {
    const calls = [];
    return {
        calls,
        pcm: {
            openWizard: (model, ctx, title) => calls.push(["openWizard", model, ctx, title]),
            openRecord: (model, id, title) => calls.push(["openRecord", model, id, title]),
            notify: (msg, opts) => calls.push(["notify", msg, opts]),
            doAction: (a) => calls.push(["doAction", a]),
        },
    };
}

let failed = 0;
function check(name, res, expectChannel, expectArg) {
    const env = makeEnv();
    handlePcmActionResult(env, res);
    const got = env.calls[0];
    const ch = got ? got[0] : "(nada)";
    const ok = ch === expectChannel && (expectArg === undefined || got[1] === expectArg);
    console.log(`${ok ? "PASS" : "FAIL"} ${name}: -> ${ch}${got && got[1] ? " ("+got[1]+")" : ""}`);
    if (!ok) {
        failed++;
        console.log(`   esperado: ${expectChannel} ${expectArg || ""} | calls=`, env.calls);
    }
}

// 1. act_window de WIZARD (target new, sin res_id) -> drawer.
check("act_window wizard -> drawer",
    { type: "ir.actions.act_window", res_model: "primate.cloud.provision.wizard",
      target: "new", context: { default_environment_id: 1 } },
    "openWizard", "primate.cloud.provision.wizard");

// 2. act_window de REGISTRO (res_id) -> navegar por la pila (no modal).
check("act_window registro -> openRecord",
    { type: "ir.actions.act_window", res_model: "primate.cloud.ec2.instance",
      res_id: 42, view_mode: "form" },
    "openRecord", "primate.cloud.ec2.instance");

// 3. display_notification -> toast.
check("display_notification -> toast",
    { type: "ir.actions.client", tag: "display_notification",
      params: { type: "success", message: "Encolado." } },
    "notify");

// 4. otra client action -> doAction (fallback).
check("client action arbitraria -> doAction",
    { type: "ir.actions.client", tag: "reload" },
    "doAction");

// 5. falsy -> no hace nada.
const envNull = makeEnv();
handlePcmActionResult(envNull, null);
console.log(`${envNull.calls.length === 0 ? "PASS" : "FAIL"} accion nula -> no-op`);
if (envNull.calls.length) failed++;

console.log(failed ? `\n${failed} FALLIDOS` : "\nTODOS OK");
process.exit(failed ? 1 : 0);
