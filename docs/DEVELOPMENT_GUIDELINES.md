# Ontwikkelrichtlijnen voor nanobot

Dit document beschrijft de architectuur, conventies en aanbevelingen voor verdere ontwikkeling van nanobot. Het is bedoeld als kompas voor bijdragers en voor iedereen die de codebase wil uitbreiden of onderhouden.

---

## Inhoudsopgave

1. [Projectfilosofie](#1-projectfilosofie)
2. [Architectuuroverzicht](#2-architectuuroverzicht)
3. [Branching en releases](#3-branching-en-releases)
4. [Ontwikkelomgeving opzetten](#4-ontwikkelomgeving-opzetten)
5. [Codeerstijl en naamgeving](#5-codeerstijl-en-naamgeving)
6. [Async-patronen](#6-async-patronen)
7. [Configuratiebeheer](#7-configuratiebeheer)
8. [Nieuwe kanalen toevoegen](#8-nieuwe-kanalen-toevoegen)
9. [Nieuwe LLM-providers toevoegen](#9-nieuwe-llm-providers-toevoegen)
10. [Tools en skills toevoegen](#10-tools-en-skills-toevoegen)
11. [Testen](#11-testen)
12. [Afhankelijkheden beheren](#12-afhankelijkheden-beheren)
13. [Docker en deployment](#13-docker-en-deployment)
14. [Bekende aandachtspunten](#14-bekende-aandachtspunten)

---

## 1. Projectfilosofie

nanobot is een lichtgewicht, persoonlijk AI-assistant-framework. De kernwaarden zijn:

- **Eenvoud boven magie.** Kies de kleinste oplossing die het werkelijke probleem oplost.
- **Leesbaarheid voor de volgende lezer.** Code schrijf je niet voor de compiler, maar voor de collega die het een jaar later leest.
- **Losse koppeling.** Houd grenzen tussen modules schoon. Vermijd onnodige nieuwe abstracties.
- **Batterijen inbegrepen, maar niet verstopt.** Functionaliteit is beschikbaar zonder dat je de interne werking hoeft te kennen, maar de interne werking is altijd te volgen.
- **Eerlijkheid over complexiteit.** Verberg complexiteit niet, maar creëer ook geen extra complexiteit.

Elke toevoeging of wijziging hoort in dit kader te passen. Vraag jezelf bij elke PR af: *maakt dit het project eenvoudiger of complexer?*

---

## 2. Architectuuroverzicht

```
CLI (typer)
    │
    ├── AgentLoop          ← sessies, commandrouting, bus-koppeling
    │       │
    │       └── AgentRunner    ← LLM-lus, tools, hooks (generiek)
    │
    ├── ChannelManager     ← kanalen starten, outbound dispatch, allowFrom
    │       │
    │       └── Kanaal-plugins  (telegram, discord, slack, …)
    │
    ├── MessageBus         ← twee asyncio.Queue's (inbound / outbound)
    │
    ├── SessionManager     ← gesprekspersistentie per session_key
    │
    ├── PROVIDERS (registry)← LLM-backends (OpenAI-compat, Anthropic, Azure, …)
    │
    ├── CronService        ← geplande jobs
    │
    └── HeartbeatService   ← periodieke workspace-runs
```

### Dataflow

```
Kanaal  →  InboundMessage  →  MessageBus (inbound queue)
                                    │
                              AgentLoop.run()
                                    │
                         CommandRouter / AgentRunner
                                    │
                         LLM + tools / skills / MCP
                                    │
                          OutboundMessage → MessageBus (outbound queue)
                                    │
                          ChannelManager → Kanaal
```

### Vuistregels voor de architectuur

- Alle communicatie tussen kanalen en de agent verloopt **uitsluitend** via de `MessageBus`. Kanalen mogen de agentlus nooit direct aanroepen.
- `AgentRunner` is de generieke LLM/tool-lus zonder domeinkennis. `AgentLoop` voegt sessies, commands en de bus-koppeling toe. Houd deze scheiding intact.
- Nieuwe functionaliteit hoort in de laag die het meest specifiek is. Een nieuw LLM-patroon → `AgentRunner`. Een nieuw slash-command → `CommandRouter`. Een nieuw platform → nieuw kanaal.

---

## 3. Branching en releases

| Branch | Doel | Stabiliteit |
|--------|------|-------------|
| `main` | Stabiele releases | Productie-klaar |
| `nightly` | Experimentele features | Kan bugs of breaking changes bevatten |

**Richtlijn:**

- Nieuwe features en refactors → `nightly`
- Bugfixes, documentatie, kleine tweaks → `main`
- Twijfel je? → `nightly`

Stabiele features worden via **cherry-pick** als aparte PR naar `main` gebracht, niet door de hele `nightly`-branch te mergen.

---

## 4. Ontwikkelomgeving opzetten

### Aanbevolen: uv

```bash
# Installeer uv (als je het nog niet hebt)
pip install uv

# Clone en installeer inclusief dev-afhankelijkheden
git clone https://github.com/HKUDS/nanobot.git
cd nanobot
uv sync --all-extras

# Tests draaien
uv run pytest tests/

# Linter
uv run ruff check nanobot/

# Formatter
uv run ruff format nanobot/
```

### Alternatief: pip

```bash
pip install -e ".[dev]"
pytest
ruff check nanobot/
```

### WhatsApp-bridge (optioneel)

De bridge vereist Node.js ≥ 20. Zie de Dockerfile voor de exacte buildstappen. Draai je lokaal zonder Docker, dan bouw je de bridge apart:

```bash
cd bridge
npm install
npm run build
```

---

## 5. Codeerstijl en naamgeving

### Formatter en linter

- **Ruff** is het enige tool voor zowel formatting als linting.
- Regellengte: **100 tekens** (E501 is uitgeschakeld, maar dat is geen vrijbrief voor onleesbaar lange regels).
- Lintregels: `E, F, I, N, W`.
- Python-versietarget: **3.11+**. Gebruik `|`-union-syntax, `match`-statements en andere moderne Python-constructies vrijelijk.

### Naamgeving

| Construct | Conventie | Voorbeeld |
|-----------|-----------|-----------|
| Modules / pakketten | `snake_case` | `openai_compat_provider.py` |
| Klassen | `PascalCase` | `ChannelManager` |
| Functies / methoden | `snake_case` | `dispatch_message()` |
| Constanten | `UPPER_SNAKE_CASE` | `MAX_RETRIES` |
| Privé-attributen | `_snake_case` | `_queue` |
| Config-keys (JSON) | `camelCase` | `"allowFrom"` |
| Config-keys (Python) | `snake_case` | `allow_from` |

### Pydantic-modellen

- Alle configuratiemodellen erven van `Base` (met `alias_generator=to_camel` en `populate_by_name=True`).
- Gebruik altijd Pydantic v2-syntax (`model_validator`, `field_validator`, enz.).
- Zet default-waarden in het model, nooit in de laadcode.

### Typing

- Gebruik altijd type-annotaties op publieke functies en methoden.
- Gebruik `TYPE_CHECKING` voor circulaire imports.
- Gebruik `slots=True` op dataclasses wanneer instanties veel worden aangemaakt (zie `AgentRunSpec` en `AgentRunResult` in `runner.py`).

### Logging

- Gebruik uitsluitend **loguru** (`from loguru import logger`).
- Gebruik `logger.debug` voor ontwikkeldetails, `logger.info` voor operationele meldingen, `logger.warning` voor onverwacht gedrag dat niet fataal is, `logger.error` / `logger.exception` voor fouten.
- Gebruik geen `print()` in de kerncode; alleen in CLI-uitvoer via `rich`.

---

## 6. Async-patronen

nanobot is volledig async. Houd je aan de volgende regels:

1. **Blokkerende I/O is verboden in de event loop.** Gebruik `asyncio.to_thread()` voor zware synchrone operaties (bestandssysteem, subprocessen die niet asyncio-native zijn).
2. **Gebruik `asyncio.gather()` voor parallelle taken**, maar wees bewust van foutafhandeling: een uitzondering in één coroutine annuleert de andere bij de default-instelling. Gebruik `return_exceptions=True` als je door wilt gaan bij fouten.
3. **Geef coroutines een naam** via de `name`-parameter van `asyncio.create_task()`. Dit maakt debugging via `asyncio.all_tasks()` veel leesbaarder.
4. **Queues zijn de enige gedeelde toestand** tussen de agentlus en kanalen. Deel geen objecten direct.
5. **Vermijd `asyncio.sleep(0)`** als tijdelijk workaround; het maskeert doorgaans een structureel probleem.

---

## 7. Configuratiebeheer

- Het primaire configuratiebestand staat op `~/.nanobot/config.json` (of een pad opgegeven via `--config`).
- Laad configuratie altijd via `load_config()` uit `nanobot.config.loader`; schrijf nooit rechtstreeks naar het bestand.
- Gebruik `set_config_path()` voor multi-instance deployments.
- Nieuwe configuratie-opties:
  1. Voeg het veld toe aan het juiste Pydantic-model in `nanobot/config/schema.py` met een verstandige default.
  2. Documenteer het veld inline (docstring of commentaar op het veld zelf).
  3. Voeg, indien relevant, een migratieregel toe in `loader._migrate_config` (bijv. bij het verplaatsen van een bestaand veld naar een andere plek in de config-structuur).
  4. Vermeld de optie in `README.md` als het een gebruikersfacing setting is.
- Omgevingsvariabelen voor provider-API-keys worden geregistreerd in `nanobot/providers/registry.py` als onderdeel van `ProviderSpec`.

---

## 8. Nieuwe kanalen toevoegen

Zie `docs/CHANNEL_PLUGIN_GUIDE.md` voor de volledige handleiding. Kort samengevat:

1. Maak een klasse die `BaseChannel` (`nanobot.channels.base`) subclasst.
2. Stel de class-attributen `name` en `display_name` in.
3. Implementeer de abstracte methoden `start()`, `stop()` en `send(msg: OutboundMessage)`. Optioneel: overschrijf `send_delta()` voor streaming-ondersteuning.
4. Gebruik `self._handle_message()` (geërfd van `BaseChannel`) om inkomende berichten op de bus te zetten — deze methode regelt ook de `allowFrom`-check.
5. Publiceer het kanaal via een `entry_point` (`nanobot.channels`) in je eigen `pyproject.toml`, of voeg het toe als built-in in `nanobot/channels/`.
6. Voeg tests toe in `tests/channels/`.

**Aandachtspunten:**

- De `allowFrom`-validatie zit in `BaseChannel.is_allowed()` en wordt automatisch aangeroepen door `_handle_message()`. Overschrijf deze methode niet in je eigen kanaal. `ChannelManager._validate_allow_from()` controleert daarnaast bij het opstarten of er geen kanalen met een lege `allowFrom`-lijst draaien.
- Zorg voor een nette `stop()`-implementatie: kanalen moeten kunnen worden gestopt zonder dat de event loop blijft hangen.
- Zware dependencies (zoals kanaalspecifieke SDKs) horen in een optionele extra in `pyproject.toml`, niet in de hoofddependencies — tenzij het kanaal zo centraal is dat het altijd aanwezig moet zijn.

---

## 9. Nieuwe LLM-providers toevoegen

De stappen staan ook bovenaan `nanobot/providers/registry.py`:

1. Voeg een `ProviderSpec`-entry toe aan de `PROVIDERS`-tuple in `nanobot/providers/registry.py` met de juiste metadata (naam, keywords, env-variabelen, backend-type, gateway-URL's, enz.).
2. Voeg een corresponderend veld toe aan `ProvidersConfig` in `nanobot/config/schema.py`.
3. Als de provider een bestaand backend gebruikt (bijv. `"openai_compat"`), ben je klaar. Maak je een **nieuw** backend-type, maak dan een providerklasse in `nanobot/providers/` (zie `openai_compat_provider.py` of `anthropic_provider.py` als voorbeeld) en registreer die in `nanobot/providers/__init__.py` via het lazy-import-mechanisme (`_LAZY_IMPORTS`).
4. Voeg tests toe in `tests/providers/`.
5. Vermeld de provider in `README.md` (providers-tabel).

**Vermijd litellm** als abstractielaag; de codebase heeft dit bewust verwijderd vanwege supply-chain-risico's.

---

## 10. Tools en skills toevoegen

### Agent-tools

Tools bevinden zich in `nanobot/agent/tools/`. Elke tool is een klasse of functie die door de agentlus kan worden aangeroepen en geregistreerd via `nanobot/agent/tools/registry.py`.

- Houd tools klein en gefocust op één verantwoordelijkheid.
- Geef duidelijke foutmeldingen terug; de LLM gebruikt deze om te corrigeren.
- Zware externe afhankelijkheden voor een tool horen in een optionele extra.

### Skills

Skills zijn Markdown-bestanden met embedded instructies, opgeslagen in `nanobot/skills/`. Ze worden door de agent geladen als onderdeel van zijn context.

- Elke skill heeft een `SKILL.md` als entrypoint.
- Shell-scripts die bij een skill horen, staan in dezelfde map.
- Houd skills herbruikbaar en generiek; specifieke logica hoort in tools, niet in skills.

### MCP-tools

MCP-tools worden geconfigureerd via de workspace en de `mcp`-dependency. Raadpleeg de README voor de configuratiesyntax. Het toevoegen van een nieuwe MCP-server vereist geen codewijzigingen in de kern.

---

## 11. Testen

### Structuur

Tests staan in `tests/` en zijn gespiegeld aan de domeinen in `nanobot/`:

```
tests/
├── agent/
├── channels/
├── cli/
├── config/
├── cron/
├── providers/
├── security/
└── tools/
```

### Regels

- Elke nieuwe module of klasse krijgt een bijbehorend testbestand.
- Elke nieuwe publieke functie krijgt minimaal één test voor het happy-pad en één voor een fout- of randgeval.
- Gebruik `pytest-asyncio` voor async tests; alle tests draaien met `asyncio_mode = "auto"`.
- Mock externe services altijd; tests mogen nooit afhankelijk zijn van netwerkverbindingen of echte API-keys.
- Gebruik `pytest.mark.parametrize` voor het testen van meerdere invoervarianten.

### Coverage draaien

```bash
uv run pytest tests/ --cov=nanobot --cov-report=term-missing
```

### Naamgeving

- Testbestanden: `test_<module>.py`
- Testfuncties: `test_<wat_wordt_getest>_<verwacht_resultaat>()` — bijv. `test_dispatch_message_retries_on_failure()`

### CI

De GitHub Actions-workflow draait op Python 3.11, 3.12 en 3.13. Zorg dat je code op alle drie werkt. De matrix-extra vereist `libolm` in de CI-omgeving; dat wordt automatisch geïnstalleerd door de workflow.

---

## 12. Afhankelijkheden beheren

### Algemene richtlijnen

- Voeg een dependency alleen toe als het een **significante hoeveelheid complexiteit vervangt** die je anders zelf zou moeten onderhouden.
- Houd versie-pins krap maar niet te star: gebruik `>=x.y, <(x+1).0` voor semver-pakketten.
- Controleer bij elke nieuwe dependency de supply-chain-reputatie (zie de verwijdering van litellm als precedent).
- Update `uv.lock` na elke dependency-wijziging via `uv lock`.

### Optionele extras

Kanaalspecifieke of use-case-specifieke dependencies horen in `[project.optional-dependencies]`, niet in de kernafhankelijkheden. Huidige extras: `wecom`, `weixin`, `matrix`, `langsmith`, `dev`.

### Node-afhankelijkheden (bridge)

- Node-afhankelijkheden staan in `bridge/package.json`.
- Wijzigingen hier vereisen ook een update van de Dockerfile en mogelijk `docker-compose.yml`.

---

## 13. Docker en deployment

### Lokaal bouwen

```bash
docker build -t nanobot .
docker compose up
```

### Services in docker-compose.yml

| Service | Doel |
|---------|------|
| `nanobot-gateway` | Hoofdproces: agent + alle kanalen |
| `nanobot-cli` | Optioneel: interactieve CLI |

### Aandachtspunten

- De `gateway.port`-instelling (standaard `18790`) is zichtbaar in de CLI en wordt gemapt in Docker, maar er draait **geen HTTP-server** op die poort in Python. Als je health checks of een management-API wilt toevoegen, moet je dit expliciet implementeren in `nanobot/cli/commands.py` (de `gateway()`-functie).
- Configuratie wordt gemount via een volume (`~/.nanobot`). Zorg dat secrets (API-keys) nooit in het image worden gebuild.
- De WhatsApp-bridge wordt in dezelfde Dockerfile-stage gebouwd: Node.js 20 wordt geïnstalleerd naast Python, waarna `npm install && npm run build` draait. Wijzigingen in de bridge vereisen een volledige rebuild.

---

## 14. Bekende aandachtspunten

### Gateway-poort zonder listener

`gateway.port` is geconfigureerd en wordt door Docker gemapt, maar er is geen daadwerkelijke TCP-listener op die poort in de Python-code. Als je een health-check-endpoint wilt toevoegen (aanbevolen voor productie), implementeer dan een minimale HTTP-server (bijv. via `aiohttp` of een simpele `asyncio`-server) in de gateway-start.

### Zware kern-afhankelijkheden

Alle kanaal-SDKs zitten in de kerndependencies. Wie alleen de CLI of één kanaal wil gebruiken, installeert toch alle SDKs. Overweeg op termijn meer kanalen naar optionele extras te verplaatsen.

### Naamgeving testbestand providers

`tests/providers/test_litellm_kwargs.py` test intern `OpenAICompatProvider`, niet litellm. Hernoem dit bestand bij een volgende gelegenheid om verwarring te voorkomen.

### Python 3.13 in CI

De CI test op Python 3.13, maar `pyproject.toml` vermeldt alleen 3.11 en 3.12 in de classifiers. Voeg 3.13 toe aan de classifiers als de CI-matrix dit officieel ondersteunt.

### Node als tweede runtime

De WhatsApp-bridge vereist Node.js 20+. Dit is een bewuste keuze maar het verhoogt de operationele complexiteit voor beheerders. Documenteer dit expliciet bij elke releasenotitie die de bridge aanraakt.

---

*Dit document is een levend document. Pas het aan als de architectuur of conventies veranderen.*
