#!/usr/bin/env node
/**
 * Helper om een Google-account opnieuw te autoriseren voor google-workspace-mcp.
 *
 * Gebruikt een vaste poort (3734) zodat je via SSH port-forward kunt werken:
 *   ssh -L 3734:localhost:3734 root@<server>
 *
 * Gebruik op de server:
 *   node scripts/google-oauth.mjs personal
 *   node scripts/google-oauth.mjs getcloudy
 *
 * Vereist: ~/.google-mcp/credentials.json met een "installed" of "web" client.
 */

import { readFile, writeFile, mkdir, access } from 'node:fs/promises';
import { homedir } from 'node:os';
import { join, dirname } from 'node:path';
import { createServer } from 'node:http';

const PORT = parseInt(process.env.OAUTH_PORT || '3734', 10);
const REDIRECT = `http://localhost:${PORT}`;
const GOOGLE_MCP_DIR = process.env.GOOGLE_MCP_DIR || join(homedir(), '.google-mcp');
const CRED_PATH = join(GOOGLE_MCP_DIR, 'credentials.json');
const ACCOUNTS_PATH = join(GOOGLE_MCP_DIR, 'accounts.json');
const TOKENS_DIR = join(GOOGLE_MCP_DIR, 'tokens');

const SCOPES = [
  'https://www.googleapis.com/auth/documents',
  'https://www.googleapis.com/auth/drive',
  'https://www.googleapis.com/auth/spreadsheets',
  'https://www.googleapis.com/auth/gmail.modify',
  'https://www.googleapis.com/auth/gmail.settings.basic',
  'https://www.googleapis.com/auth/calendar',
  'https://www.googleapis.com/auth/presentations',
  'https://www.googleapis.com/auth/forms.body',
  'https://www.googleapis.com/auth/forms.responses.readonly',
];

async function main() {
  const accountName = process.argv[2];
  if (!accountName || !/^[a-zA-Z0-9_-]+$/.test(accountName)) {
    console.error('Usage: node scripts/google-oauth.mjs <account-name>');
    process.exit(1);
  }

  const credRaw = await readFile(CRED_PATH, 'utf8').catch(() => {
    throw new Error(`Credentials niet gevonden op ${CRED_PATH}`);
  });
  const creds = JSON.parse(credRaw);
  const key = creds.installed ?? creds.web;
  if (!key) throw new Error(`Geen installed/web client in ${CRED_PATH}`);

  const { client_id, client_secret } = key;

  // Build OAuth URL by hand — geen dependencies nodig
  const params = new URLSearchParams({
    client_id,
    redirect_uri: REDIRECT,
    response_type: 'code',
    scope: SCOPES.join(' '),
    access_type: 'offline',
    prompt: 'consent',
  });
  const authUrl = `https://accounts.google.com/o/oauth2/v2/auth?${params.toString()}`;

  console.log('\n========================================');
  console.log(`Open deze URL in je browser (log in als het juiste Google-account):\n`);
  console.log(authUrl);
  console.log(`\nServer luistert op ${REDIRECT} voor de callback...`);
  console.log('========================================\n');

  const code = await new Promise((resolve, reject) => {
    const server = createServer((req, res) => {
      try {
        const url = new URL(req.url ?? '', REDIRECT);
        const c = url.searchParams.get('code');
        const err = url.searchParams.get('error');
        if (err) {
          res.writeHead(400, { 'Content-Type': 'text/html' });
          res.end(`<h1>OAuth fout: ${err}</h1>`);
          server.close();
          reject(new Error(err));
          return;
        }
        if (c) {
          res.writeHead(200, { 'Content-Type': 'text/html' });
          res.end(
            `<h1>Geautoriseerd voor "${accountName}"</h1><p>Je kunt dit tabblad sluiten.</p>`,
          );
          server.close();
          resolve(c);
          return;
        }
        res.writeHead(404);
        res.end();
      } catch (e) {
        server.close();
        reject(e);
      }
    });
    server.listen(PORT, '127.0.0.1');
    setTimeout(() => {
      server.close();
      reject(new Error('Timeout na 5 minuten zonder callback'));
    }, 5 * 60 * 1000);
  });

  // Exchange code voor tokens
  const tokenResp = await fetch('https://oauth2.googleapis.com/token', {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({
      code,
      client_id,
      client_secret,
      redirect_uri: REDIRECT,
      grant_type: 'authorization_code',
    }),
  });
  if (!tokenResp.ok) {
    throw new Error(`Token exchange faalde: ${tokenResp.status} ${await tokenResp.text()}`);
  }
  const tokens = await tokenResp.json();
  if (!tokens.refresh_token) {
    throw new Error('Geen refresh_token ontvangen — revoke je app-toegang in Google Account en probeer opnieuw.');
  }

  // Schrijf token en account-config in hetzelfde formaat als google-workspace-mcp
  await mkdir(TOKENS_DIR, { recursive: true });
  const tokenPath = join(TOKENS_DIR, `${accountName}.json`);
  const tokenPayload = {
    type: 'authorized_user',
    client_id,
    client_secret,
    refresh_token: tokens.refresh_token,
  };
  await writeFile(tokenPath, JSON.stringify(tokenPayload, null, 2));

  // Update accounts.json
  let config = { accounts: {}, credentialsPath: CRED_PATH };
  try {
    config = JSON.parse(await readFile(ACCOUNTS_PATH, 'utf8'));
  } catch {}
  config.accounts[accountName] = {
    name: accountName,
    tokenPath,
    addedAt: new Date().toISOString(),
  };
  config.credentialsPath = CRED_PATH;
  await mkdir(dirname(ACCOUNTS_PATH), { recursive: true });
  await writeFile(ACCOUNTS_PATH, JSON.stringify(config, null, 2));

  console.log(`\nAccount "${accountName}" opgeslagen:`);
  console.log(`  Token:    ${tokenPath}`);
  console.log(`  Accounts: ${ACCOUNTS_PATH}\n`);
}

main().catch((err) => {
  console.error('\nFOUT:', err.message);
  process.exit(1);
});
