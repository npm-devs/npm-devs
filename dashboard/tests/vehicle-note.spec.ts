import { expect, test, type Page } from "@playwright/test";

// La Domain API se sustituye con page.route (con la misma regla de X-Notes-Key que el backend): aquí se prueba
// lo que ve el operador, no Atomic Memory.
const CLAVE = "clave-operador";
type Nota = { alias: string; texto: string; procedencia: "human_reported"; guardada_en: string | null };

async function domainApi(page: Page, opts: { habilitada?: boolean; inicial?: Record<string, string>; falla?: boolean } = {}) {
  const notas = new Map(Object.entries(opts.inicial ?? {}));
  const puts: unknown[] = [];
  const keys: (string | null)[] = [];
  await page.route(/\/v1\/vehicles\/(T\d{2})\/note$/, async (route) => {
    const req = route.request();
    const alias = req.url().match(/vehicles\/(T\d{2})\//)![1];
    const method = req.method();
    if (method === "OPTIONS") return route.fulfill({ status: 204 });
    const habilitada = opts.habilitada ?? true;
    if (!habilitada) return route.fulfill({ json: { habilitada: false, nota: null } });
    const key = (await req.headerValue("x-notes-key")) ?? null;
    keys.push(key);
    if (key !== CLAVE) return route.fulfill({ status: 401, json: { detail: "Clave de notas del operador ausente o incorrecta" } });
    if (opts.falla && method !== "GET") {
      return route.fulfill({ status: 503, json: { detail: "No se guardó la nota: Atomic Memory no respondió a tiempo" } });
    }
    if (method === "PUT") {
      const body = req.postDataJSON();
      puts.push(body);
      notas.set(alias, body.texto);
    }
    if (method === "DELETE") notas.delete(alias);
    const texto = notas.get(alias);
    const nota: Nota | null = texto ? { alias, texto, procedencia: "human_reported", guardada_en: "2026-10-20T15:00:00Z" } : null;
    await route.fulfill({ json: { habilitada: true, nota } });
  });
  return { puts, keys };
}

async function abrir(page: Page, alias: string) {
  await page.goto("/");
  await expect(page.getByText("Cargando…")).toBeHidden();
  await page.locator(`button.vehicle[data-alias="${alias}"]`).click();
  return page.getByRole("region", { name: `Nota revisada de ${alias}` });
}

async function desbloquear(page: Page, alias: string, clave = CLAVE) {
  const panel = page.getByRole("region", { name: `Nota revisada de ${alias}` });
  await panel.getByLabel("Clave de notas del operador").fill(clave);
  await panel.getByRole("button", { name: "Desbloquear" }).click();
}

test("sin la clave del operador la nota no se muestra; con la clave se recuerda", async ({ page }) => {
  const api = await domainApi(page, { inicial: { T07: "Sobreconsumo revisado: iba con 4 t más. Sin acción." } });
  const panel = await abrir(page, "T07");

  await expect(panel.getByLabel("Clave de notas del operador")).toBeVisible();
  await expect(panel).not.toContainText("Sobreconsumo revisado");
  expect(api.keys).toEqual([null]);

  await desbloquear(page, "T07");
  await expect(panel).toContainText("Sobreconsumo revisado: iba con 4 t más. Sin acción.");
  await expect(panel).toContainText("Memoria del operador");
});

test("una clave incorrecta no desbloquea", async ({ page }) => {
  await domainApi(page, { inicial: { T07: "Nota privada del operador." } });
  const panel = await abrir(page, "T07");

  await desbloquear(page, "T07", "otra");

  await expect(panel.getByRole("alert")).toHaveText("Clave de notas incorrecta.");
  await expect(panel).not.toContainText("Nota privada del operador.");
});

test("la clave queda en la pestaña al cambiar de vehículo y se olvida al recargar", async ({ page }) => {
  await domainApi(page, { inicial: { T04: "Ralentí revisado." } });
  await abrir(page, "T07");
  await desbloquear(page, "T07");
  await expect(page.getByRole("region", { name: "Nota revisada de T07" })).toContainText("Sin nota revisada para T07.");

  await page.locator('button.vehicle[data-alias="T04"]').click();
  await expect(page.getByRole("region", { name: "Nota revisada de T04" })).toContainText("Ralentí revisado.");
  expect(await page.evaluate(() => JSON.stringify({ ...localStorage }) + JSON.stringify({ ...sessionStorage }))).not.toContain(CLAVE);

  await page.reload();
  await expect(page.getByText("Cargando…")).toBeHidden();
  await page.locator('button.vehicle[data-alias="T04"]').click();
  await expect(page.getByRole("region", { name: "Nota revisada de T04" }).getByLabel("Clave de notas del operador")).toBeVisible();
});

test("guardar exige el consentimiento explícito y luego se corrige y se borra", async ({ page }) => {
  const api = await domainApi(page);
  const panel = await abrir(page, "T04");
  await desbloquear(page, "T04");

  await panel.getByRole("button", { name: "Agregar nota" }).click();
  await panel.getByRole("textbox").fill("Ralentí de noche confirmado con el conductor.");
  const guardar = panel.getByRole("button", { name: "Guardar nota" });
  await expect(guardar).toBeDisabled();
  await panel.getByRole("checkbox", { name: /autorizo guardarla en Atomic Memory/ }).check();
  await guardar.click();
  await expect(panel).toContainText("Ralentí de noche confirmado con el conductor.");
  expect(api.puts).toEqual([{ texto: "Ralentí de noche confirmado con el conductor.", consentimiento: true }]);

  await panel.getByRole("button", { name: "Corregir" }).click();
  await panel.getByRole("textbox").fill("Ralentí de noche: el conductor durmió con el motor prendido.");
  await panel.getByRole("checkbox").check();
  await panel.getByRole("button", { name: "Guardar nota" }).click();
  await expect(panel).toContainText("el conductor durmió con el motor prendido");

  await panel.getByRole("button", { name: "Borrar" }).click();
  await expect(panel).toContainText("Sin nota revisada para T04.");
});

test("si Atomic Memory falla, dice que la nota no se guardó y conserva el texto", async ({ page }) => {
  await domainApi(page, { falla: true });
  const panel = await abrir(page, "T07");
  await desbloquear(page, "T07");

  await panel.getByRole("button", { name: "Agregar nota" }).click();
  await panel.getByRole("textbox").fill("Parada larga en Nazca.");
  await panel.getByRole("checkbox").check();
  await panel.getByRole("button", { name: "Guardar nota" }).click();

  await expect(panel.getByRole("alert")).toHaveText("No se guardó la nota: Atomic Memory no respondió a tiempo");
  await expect(panel.getByRole("textbox")).toHaveValue("Parada larga en Nazca.");
});

test("sin notas configuradas en el servidor el panel no aparece ni pide clave", async ({ page }) => {
  await domainApi(page, { habilitada: false });
  await abrir(page, "T07");

  await expect(page.locator('button.vehicle[data-alias="T07"]')).toHaveAttribute("aria-pressed", "true");
  await expect(page.getByRole("region", { name: /Nota revisada/ })).toHaveCount(0);
});

test("una respuesta inválida no rompe el dashboard ni se presenta como una nota vacía", async ({ page }) => {
  await page.route(/\/v1\/vehicles\/T07\/note$/, route => route.fulfill({ json: { habilitada: true, nota: { texto: 42 } } }));
  await abrir(page, "T07");
  await expect(page.getByRole("region", { name: "Nota revisada de T07" }).getByRole("alert"))
    .toHaveText("No se pudo leer la nota: respuesta inválida del servidor");
  await expect(page.locator('button.vehicle[data-alias="T07"]')).toHaveAttribute("aria-pressed", "true");
});
