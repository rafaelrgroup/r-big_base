/* Synthetic browser protocol proof. Local static server, mocked API, no database. */
import { chromium, expect } from '@playwright/test';
import { createServer } from 'node:http';
import { readFileSync, writeFileSync } from 'node:fs';
import { resolve, extname } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = fileURLToPath(new URL('./dist/', import.meta.url));
const server = createServer((req, res) => {
  const path = resolve(root, '.' + new URL(req.url, 'http://localhost').pathname);
  if (!(path === root.slice(0, -1) || path.startsWith(root))) { res.writeHead(403); res.end(); return; }
  try {
    const actual = path === root.slice(0, -1) ? resolve(root, 'index.html') : path;
    res.setHeader('Content-Type', ({ '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml' })[extname(actual)] || 'application/octet-stream');
    res.end(readFileSync(actual));
  } catch { res.writeHead(404); res.end(); }
});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
const origin = 'http://127.0.0.1:' + server.address().port;
const browser = await chromium.launch({ headless: true, args: ['--disable-dev-shm-usage'] });
const checks = [], errors = [];
const identity = { csrf: 'synthetic-csrf', user: { id: 'synthetic-user', username: 'synthetic-ui', role: 'admin', permissions: ['read', 'enrich', 'validate', 'admin', 'export'] } };
const forbidden = /^\/(stats|people|companies|bulk-queries|imports|saved-searches|search\/catalog|admin\/fields)(\/|$)/;

async function session({ deployed = true, login = false, healthFails = false, search = false } = {}) {
  const context = await browser.newContext({ viewport: { width: 1280, height: 960 }, serviceWorkers: 'block' });
  const page = await context.newPage(); page.setDefaultTimeout(10000); page.on('pageerror', e => errors.push(e.message));
  const calls = [], control = { authenticated: !login, healthFails, search,
    searches: [], releases: [], migration: { status: 'not_reported', phase: null, processed: null, total: null, progress_percent: null } };
  await context.route('**/api/v1/**', async route => {
    const path = new URL(route.request().url()).pathname.replace('/api/v1', '');
    calls.push({ path, method: route.request().method() });
    if (path === '/canonical/status' && control.search)
      await new Promise(resolve => setTimeout(resolve, 20));
    let body = {}, status = 200;
    if (path === '/health') {
      if (control.healthFails) { body = { detail: 'Synthetic unavailable' }; status = 503; }
      else body = deployed ? { status: 'ok', runtime: 'deployed', storage_mode: 'canonical', environment: 'staging', canonical_connected: true,
        synthetic: false, local_person_data_enabled: false, writes_enabled: true, migration_complete: null, migration: control.migration } : { status: 'ok', environment: 'isolated-development', production_connected: false };
    } else if (path === '/auth/me') { body = control.authenticated ? identity : { detail: 'Login required' }; status = control.authenticated ? 200 : 401; }
    else if (path === '/auth/login') body = { challenge: 'synthetic-challenge' };
    else if (path === '/auth/otp') { control.authenticated = true; body = identity; }
    else if (path === '/auth/logout') { control.authenticated = false; body = { ok: true }; }
    else if (path === '/canonical/status') body = { enabled: true, writes_enabled: true, can_enrich: true, can_validate: true, can_administer_catalog: true,
      runtime: deployed ? 'deployed' : null, search_enabled: control.search, search_coverage: 'canonical_snapshot',
      search_fields: { identity: ['name'], phone: ['number', 'type'], address: ['city', 'street', 'postal_code'],
        document: ['number'], email: ['email'], activity: ['code'], custom: ['value'] } };
    else if (path === '/canonical/people/search') {
      const raw = route.request().postData(); control.searches.push(raw); const query = JSON.parse(raw);
      const id = query.cursor ? '00000000-0000-4000-8000-000000000002' : '00000000-0000-4000-8000-000000000001';
      body = { items: [{ id, record_version: 1, indexed_version: 1, indexing_pending: false }],
        total: { value: 2, relation: 'eq' }, has_more: !query.cursor,
        next_cursor: query.cursor ? null : 'synthetic-next', release_cursor: 'synthetic-release' };
    } else if (path === '/canonical-search/close') { control.releases.push(JSON.parse(route.request().postData())); body = { released: true }; }
    else if (path === '/canonical/fields') body = { items: [], next_after: null };
    else if (path === '/admin/sources') body = { items: [{ id: 'manual', name: 'Synthetic manual' }] };
    else if (['/admin/users', '/admin/api-keys', '/admin/fields', '/audit'].includes(path)) body = { items: [] };
    else if (path === '/search/catalog') body = {};
    else if (path === '/stats') body = {};
    else if (path === '/people/search') body = { items: [], total: 0 };
    else { body = { detail: 'Unexpected synthetic request' }; status = 503; }
    await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
  });
  await page.goto(origin);
  return { context, page, calls, control };
}

try {
  const first = await session(); const { page, calls, control } = first;
  await expect(page.getByRole('heading', { name: 'Consulta da base unificada', exact: true })).toBeVisible();
  const nav = page.locator('nav');
  for (const name of ['Pessoas', 'Empresas', 'Pesquisas salvas', 'Consulta em massa', 'Importações'])
    await expect(nav.getByRole('button', { name, exact: true })).toHaveCount(0);
  await expect(page.getByText('Somente dados de teste', { exact: true })).toHaveCount(0);
  await expect(page.getByText('O progresso da migração ainda não foi informado pelo serviço.', { exact: true })).toBeVisible();
  await expect(page.locator('progress')).toHaveCount(0);
  checks.push('Deployed staging shows canonical UI, hides local dataset screens and does not invent progress');
  await page.getByRole('button', { name: 'Administração', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Usuários', exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Campos adicionais', exact: true })).toHaveCount(0);
  await page.getByRole('button', { name: 'Fontes', exact: true }).click();
  await expect(page.getByText('Synthetic manual', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Consulta canônica', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Criar cadastro', exact: true })).toBeVisible();
  await page.getByLabel('Grupo', { exact: true }).first().selectOption('custom');
  await expect(page.getByLabel('Catálogo da definição')).toHaveValue('postgresql');
  await expect(page.getByLabel('Catálogo da definição').locator('option')).toHaveCount(1);
  await page.getByRole('button', { name: 'Adicionar observação', exact: true }).click();
  await page.getByLabel('Grupo', { exact: true }).nth(1).selectOption('custom');
  for (const select of await page.getByLabel('Catálogo da definição').all()) await expect(select).toHaveValue('postgresql');
  checks.push('Administration remains available and additional fields bind only PostgreSQL in every new entry');
  control.migration = { status: 'running', phase: 'importing', processed: 125, total: 10000, progress_percent: 1.25 };
  await page.getByRole('button', { name: 'Atualizar andamento', exact: true }).click();
  await expect(page.locator('progress')).toHaveAttribute('value', '1.25');
  await expect(page.getByText('Importando registros', { exact: true })).toBeVisible();
  control.migration = { status: 'needs_attention', phase: 'pilot', processed: 125, total: 10000, progress_percent: 1.25, freshness: 'fresh' };
  await page.getByRole('button', { name: 'Atualizar andamento', exact: true }).click();
  await expect(page.getByText('Revisão necessária', { exact: true })).toBeVisible();
  checks.push('Failure status takes precedence over pilot phase');
  control.migration = { ...control.migration, freshness: 'stale' };
  await page.getByRole('button', { name: 'Atualizar andamento', exact: true }).click();
  await expect(page.getByText('Andamento desatualizado. Aguardando nova publicação do serviço.', { exact: true })).toBeVisible();
  await expect(page.getByText(/Última contagem informada de registros de origem:/)).toBeVisible();
  await expect(page.locator('progress')).toHaveCount(0);
  control.migration = { ...control.migration, status: 'running', freshness: 'fresh' };
  await page.getByRole('button', { name: 'Atualizar andamento', exact: true }).click();
  await expect(page.getByText('Piloto de migração', { exact: true })).toBeVisible();
  await expect(page.locator('progress')).toHaveAttribute('value', '1.25');
  checks.push('Stale publication hides progress, labels historical counters and recovers after a fresh report');
  control.healthFails = true;
  await page.getByRole('button', { name: 'Atualizar andamento', exact: true }).click();
  await expect(page.getByText('Não foi possível atualizar o andamento. Reconecte e tente novamente.', { exact: true })).toBeVisible();
  await expect(page.locator('progress')).toHaveCount(0);
  if (calls.some(c => forbidden.test(c.path))) throw Error('Deployed UI called a forbidden local-dataset route');
  checks.push('Progress uses measured data, hides stale measurements after failure, and no local data route is called');
  await first.context.close();

  const auth = await session({ login: true });
  await auth.page.getByLabel('Usuário', { exact: true }).fill('synthetic-ui');
  await auth.page.getByLabel('Senha', { exact: true }).fill('SYNTHETIC-ONLY-PASSWORD');
  await auth.page.getByRole('button', { name: 'Continuar', exact: true }).click();
  await auth.page.getByLabel('Código de seis dígitos').fill('123456');
  await auth.page.getByRole('button', { name: 'Confirmar acesso', exact: true }).click();
  await expect(auth.page.getByRole('heading', { name: 'Consulta da base unificada', exact: true })).toBeVisible();
  checks.push('Password plus OTP UI sequence is preserved with synthetic authentication responses');
  await auth.context.close();

  const blocked = await session({ healthFails: true });
  await expect(blocked.page.getByText('Não foi possível conferir o ambiente do painel.', { exact: true })).toBeVisible();
  if (blocked.calls.some(c => forbidden.test(c.path))) throw Error('Health failure fell back to local dataset');
  checks.push('Initial health failure blocks storage fallback');
  await blocked.context.close();

  const dev = await session({ deployed: false });
  await expect(dev.page.locator('nav').getByRole('button', { name: 'Pessoas', exact: true })).toBeVisible();
  await expect(dev.page.locator('nav').getByRole('button', { name: 'Importações', exact: true })).toBeVisible();
  await expect(dev.page.getByText('Somente dados de teste', { exact: true })).toBeVisible();
  await expect.poll(() => dev.calls.some(c => c.path === '/people/search')).toBe(true);
  checks.push('Development navigation and local dataset requests remain unchanged');
  await dev.context.close();

  const searchable = await session({ search: true });
  const query = searchable.page.getByRole('region', { name: 'Pesquisa canônica', exact: true });
  const firstFilter = query.getByRole('group', { name: 'Filtro 1', exact: true });
  await firstFilter.getByLabel('Valor', { exact: true }).fill('PESSOA SINTETICA');
  await query.getByRole('button', { name: 'Adicionar filtro', exact: true }).click();
  const secondFilter = query.getByRole('group', { name: 'Filtro 2', exact: true });
  await secondFilter.getByLabel('Informação', { exact: true }).selectOption('phone');
  await secondFilter.getByLabel('Correspondência', { exact: true }).selectOption('eq');
  await secondFilter.getByLabel('Valor', { exact: true }).fill('00000000000');
  await secondFilter.getByLabel('Validade', { exact: true }).selectOption('false');
  await secondFilter.getByLabel('WhatsApp', { exact: true }).selectOption('false');
  await query.getByLabel('Incluir informações invalidadas', { exact: true }).check();
  await query.getByRole('button', { name: 'Pesquisar cadastros', exact: true }).click();
  await expect(query.getByRole('button', { name: 'Abrir ficha deste resultado', exact: true })).toHaveCount(1);
  const sent = JSON.parse(searchable.control.searches.at(-1));
  expect(sent.filters.all[1].flags).toEqual({ valid: false, is_whatsapp: false });
  expect(sent.include_invalid).toBe(true);
  await query.getByRole('button', { name: 'Próxima página de resultados', exact: true }).click();
  await expect(query.getByText('00000000-0000-4000-8000-000000000002', { exact: true })).toBeVisible();
  expect(JSON.parse(searchable.control.searches.at(-1)).cursor).toBe('synthetic-next');
  await query.getByRole('button', { name: 'Encerrar pesquisa', exact: true }).click();
  await expect.poll(() => searchable.control.releases.length).toBeGreaterThan(0);
  checks.push('Deployed search submits multiple filters with false flags, pages results and releases its cursor');

  await query.getByLabel('Combinar filtros', { exact: true }).selectOption('same_item');
  const beforeInvalid = searchable.control.searches.length;
  await query.getByRole('button', { name: 'Pesquisar cadastros', exact: true }).click();
  await expect(query.getByRole('alert')).toContainText('mesmo tipo');
  expect(searchable.control.searches.length).toBe(beforeInvalid);
  await firstFilter.getByLabel('Informação', { exact: true }).selectOption('phone');
  await firstFilter.getByLabel('Valor', { exact: true }).fill('00000000000');
  await query.getByRole('button', { name: 'Pesquisar cadastros', exact: true }).click();
  await expect.poll(() => searchable.control.searches.length).toBe(beforeInvalid + 1);
  expect(JSON.parse(searchable.control.searches.at(-1)).filters.same_item.kind).toBe('phone');
  checks.push('Same-item filter rejects mismatched types and preserves correlated phone criteria');

  await query.getByRole('button', { name: 'Remover filtro 2', exact: true }).click();
  await query.getByLabel('Combinar filtros', { exact: true }).selectOption('all');
  await firstFilter.getByLabel('Informação', { exact: true }).selectOption('activity');
  await firstFilter.getByLabel('Tipo do valor', { exact: true }).selectOption('number');
  await firstFilter.getByLabel('Valor', { exact: true }).fill('900719925474099312345');
  await query.getByRole('button', { name: 'Pesquisar cadastros', exact: true }).click();
  await expect.poll(() => searchable.control.searches.at(-1)).toContain('"value":900719925474099312345');
  await expect(firstFilter.getByLabel('Informação', { exact: true }).locator('option[value="custom"]')).toHaveCount(0);
  checks.push('Search preserves large numeric literals and does not offer unbound additional-field selectors');

  searchable.control.search = false;
  await searchable.page.reload();
  await expect(searchable.page.getByText('A pesquisa por filtros aguarda a validação do índice. A localização exata continua disponível abaixo.', { exact: true })).toBeVisible();
  await searchable.context.close();
  checks.push('Revoked search capability removes the active filter form after availability refresh');
  if (errors.length) throw Error('Browser page errors: ' + errors.length);
  writeFileSync(new URL('./deployed-ui-test-report.json', import.meta.url), JSON.stringify({ status: 'passed', synthetic: true, backend_exercised: false, checks, page_errors: errors, at: new Date().toISOString() }, null, 2));
  console.log(JSON.stringify({ status: 'passed', checks: checks.length, page_errors: errors.length }));
} finally { await browser.close(); await new Promise(resolve => server.close(resolve)); }
