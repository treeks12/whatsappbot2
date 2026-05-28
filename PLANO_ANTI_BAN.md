# Plano Anti-Ban — WhatsApp Bot v2

Plano priorizado para reduzir as chances de a Meta desativar a conta de WhatsApp das vendedoras
que usam este bot. Foco em ataques aos vetores reais de risco do nosso cenário, não em
checkboxes genéricos.

> **Premissa honesta:** Baileys/Evolution é caminho não-oficial. Nenhuma medida torna a conta
> intocável. Tudo aqui é redução de probabilidade. A meta é tornar o número um falso positivo
> caro de identificar, não invisível.

---

## Sumário executivo

Por ordem real de impacto no nosso cenário (lista madura, conteúdo idêntico, vendedora envia
3-5 fotos + 1 caption, hospedado em VPS OVH/EUA, números BR), os 5 vetores que mais contam:

| # | Vetor | Quem ataca | Onde resolve |
|---|---|---|---|
| 1 | Destinatário errado denuncia (recyclado / mudou número) | Blacklist + histórico de delivery/reply | Código + operacional |
| 2 | Muitos destinatários únicos em curto período | Cota diária por número, ritmo, janela horária, particionar campanha em dias | Código |
| 3 | Conteúdo byte-a-byte idêntico para todos | Recompressão JPEG por destinatário + spintax na saudação | Código |
| 4 | Conta usada só para automação | Comportamento natural do número (uso humano, conversas, foto, perfil) | Operacional |
| 5 | Falta de opt-out (LGPD + reduz "report spam") | Auto-blacklist em palavras de cancelamento + linha de opt-out na campanha | Código + jurídico |

Itens secundários, mas que valem dinheiro:

- Detecção de shadowban e auto-pause em vez de continuar e queimar mais.
- Suporte a `@lid` (mudança de protocolo do WhatsApp em curso, vai virar bug se ignorado).
- 2-step verification ativada na conta de WhatsApp da vendedora (defesa contra SIM-swap).
- Validação de número via Evolution antes de enviar.

Itens **não** prioritários, embora muito comentados:

- Trocar VPS para Brasil. IP/ASN é fator **secundário** segundo dados de quem opera comercialmente.
  Z-API testou rotação de IP em ciclos de 15/30/45/60 dias e não viu redução significativa.
- User-agent / fingerprint do Baileys. Detecção é do lado servidor, com base em padrão de envio.
- Pairing code vs QR. Indiferente.

---

## Referências usadas

Tudo abaixo foi cruzado com pelo menos uma destas fontes. Não é palpite.

| Fonte | O que valida |
|---|---|
| Z-API — *Blocks and Bans (2026)* | Ranking real dos fatores de ban; relevância secundária de IP/ASN; descrição de shadowban e como detectar via webhook de erro |
| Z-API — *Best Practices Guide for Using WhatsApp via API* | 10 pilares operacionais (comportamento natural, equilíbrio enviado/recebido, variação de conteúdo, intervalos, opt-out, warmup, separação de números) |
| Z-API — *Lid* | Mudança de protocolo: WhatsApp passou a retornar `@lid` em vez de número em alguns webhooks, especialmente quando o usuário ativou privacidade do número |
| Wasenderapi — *Stop Getting Banned 2025* | Estratégia "reply-first", warmup em fases (semana 1/2/3), delays aleatórios 10-45s, evitar log-in/log-out repetido |
| Wasenderapi — *Evolution API 2026 Guide* | Caps empíricos: número novo 20-50 msgs/dia, warmed 80-200 msgs/dia. Pausa de 10-15 min a cada 50 envios |
| WhatsApp Help Center — *About registration and two-step verification* | Confirmação de que se um atacante ativar PIN de 2-step verification, a vítima precisa esperar 7 dias para resetar |
| ANPD / messagecentral.com — *LGPD WhatsApp Business 2026* | Multas até R$50M, opt-out obrigatório em até 24h, consentimento separado de e-mail, retenção de logs (marketing 2 anos, transacional 5 anos, opt-out 5 anos pós-revogação) |

---

## Mapa do código atual (resumo)

Para ancorar as mudanças propostas:

- `app/scheduler.py::CampaignScheduler._run_campaign` — laço principal do disparo, aplica `profile`,
  pausas, controla pause/resume/cancel, monta progresso no Telegram.
- `app/scheduler.py::CampaignScheduler._send_contact` — envia as mídias e o texto. Hoje cacheia
  `media_base64` por path (todos recebem o mesmo arquivo). Substitui `{nome}` se houver.
- `app/profiles.py` — perfis `confianca_100`, `precaucao_100`, `loja_100`, `humano_100`, `normal`,
  com delays aleatórios e pausas a cada N contatos.
- `app/evolution.py::EvolutionClient` — `send_text`, `send_media`, `connection_state`,
  `ensure_fresh_qr`, `wait_until_open`. **Sem** chamada de presença (`composing`) ou de
  `whatsappNumbers`.
- `app/db.py` — SQLite via `aiosqlite`. Tabelas que importam aqui: `vendors`, `campaigns`,
  `campaign_contacts`, `contacts` (telefone + nome), `media`. **Sem** webhook entrante,
  **sem** histórico de delivery/read/reply, **sem** blacklist.
- `app/config.py` — settings. `default_profile=humano_100` no código, `precaucao_100` no README
  (divergente). `MAX_*_CLIENTS_PER_CAMPAIGN=0` (sem cota). `SEND_WINDOW=` vazio.
- `app/evolution_power.py` — controla container Evolution (subir/desligar). Não relogata.
- Webhook de Evolution: **não configurado**. Sem ele, não temos delivered/read/reply.

---

## Plano em fases

### Fase 0 — Ajustes operacionais sem código (fazer agora)

Ganho desproporcional ao esforço.

| Item | Como | Por que |
|---|---|---|
| `.env`: `SEND_WINDOW=09:00-19:00` | Editar `.env`, reiniciar container | Z-API: comportamento fora de horário comercial é sinal clássico de bot |
| `.env`: `DEFAULT_PROFILE=humano_100` | Edita `.env`. README está dizendo `precaucao_100`, código diz `humano_100`. Padronizar | Convergir documentação com realidade |
| `.env`: `MAX_PRECAUTION_CLIENTS_PER_CAMPAIGN=120`, `MAX_TRUSTED_CLIENTS_PER_CAMPAIGN=250` | Edita `.env` | Wasenderapi: empírico para warmed numbers (80-200/dia). Cap por campanha aproxima cap por dia se for 1 campanha/dia |
| Ativar 2-step verification (PIN) na conta de WhatsApp de cada vendedora | Manual no celular: WhatsApp → Configurações → Conta → Verificação em duas etapas | Defesa contra SIM-swap. Se atacante tomar o número, ainda precisa do PIN; sem PIN, espera 7 dias para resetar |
| Foto de perfil + nome de loja + descrição preenchidos em todas as contas | Manual no celular | Z-API #1: número que aparenta uso real é menos flagado |
| Cada vendedora usa o número também para conversa real (família, grupos, atendimento manual) | Operacional | Z-API #1 e #2: equilíbrio enviado/recebido é o segundo fator mais relevante |
| Manter WhatsApp Web aberto também num navegador residencial brasileiro algumas horas/semana | Manual | Mistura de "linked devices" residencial BR + datacenter EUA reduz a estranheza geográfica |
| Não fazer log-out / log-in da Evolution toda hora | Já está correto: `/desconectar` desliga container em vez de logout | Wasenderapi confirma que log-out/log-in repetidos são suspeitos |
| Rever `caption` padrão das vendedoras: nada de bit.ly/encurtador, evitar CAIXA ALTA, evitar "PROMOÇÃO IMPERDÍVEL", "GRÁTIS HOJE", "ÚLTIMO DIA" | Treino com vendedoras | Z-API: keywords sensíveis e padrão "e-mail marketing" são triggers |
| Disparar 3-4 dias por semana, não 7 | Combinar com vendedoras | Padrão de uso "todo dia volume alto" é suspeito |

**Custo:** zero. **Impacto:** alto. Fazer hoje.

---

### Fase 1 — Defesa contra denúncia do destinatário (blacklist + histórico)

**Objetivo:** o vetor #1 do nosso cenário é a vendedora mandar para alguém que não devia.
Blacklist persistente, alimentada por sinais automáticos e ações manuais, é a camada mais
poderosa que dá para colocar.

#### 1.1 Tabela `blacklist`

```sql
CREATE TABLE blacklist (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    phone TEXT NOT NULL,                -- normalizado igual aos contatos
    chat_lid TEXT,                      -- @lid quando conhecido
    vendor_id INTEGER,                  -- NULL = global (admin)
    reason_code TEXT NOT NULL,          -- enum abaixo
    reason_note TEXT,                   -- texto livre
    source TEXT NOT NULL,               -- 'manual'|'auto_no_delivery'|'shadowban_signal'|'replied_stop'|'imported'
    added_at TIMESTAMP NOT NULL,
    added_by_user_id INTEGER,           -- telegram user id
    UNIQUE(phone, vendor_id)
);
CREATE INDEX idx_blacklist_phone ON blacklist(phone);
CREATE INDEX idx_blacklist_vendor ON blacklist(vendor_id);
```

`reason_code` aceitos:
- `manual_request` — cliente pediu para parar (vendedora marcou)
- `wrong_person` — número virou de outra pessoa
- `bounced` — sem entrega há N tentativas
- `replied_stop` — resposta automática detectada por palavra-chave
- `reported_signal` — sintoma de denúncia (desconexão suspeita logo após envio)
- `manual_other` — vendedora explica em `reason_note`
- `imported` — veio de arquivo

#### 1.2 Filtro em três camadas

| Camada | Ponto no código | Efeito |
|---|---|---|
| Importação | `app/csv_utils.py` (parser) + chamadas em `telegram_bot.py` que adicionam contatos a uma lista | Número em blacklist nem entra na lista. Bot avisa: "ignorei N contatos em blacklist" |
| Montagem da campanha | `Database` antes de chamar `start_campaign` | Filtra fila ao enfileirar. Mostra resumo no Telegram |
| Imediatamente antes do envio | `CampaignScheduler._run_campaign`, dentro do laço, após `next_pending_contact` | Rede final. Permite adicionar à blacklist *durante* a campanha e o próximo já não recebe |

#### 1.3 Comandos novos no Telegram

- `/blacklist <numero>` — adiciona um número (escopo: vendedora atual)
- `/blacklist <numero> motivo aqui` — adiciona com nota
- `/blacklist_arquivo` — vendedora envia CSV/lista no formato `telefone[,motivo]`
- `/blacklist_remover <numero>` — remove (caso erro)
- `/blacklist_listar` — paginado
- Botão inline na mensagem de progresso: **"Adicionar último à blacklist"** — adiciona o último
  contato da campanha à blacklist e pede motivo

#### 1.4 Custo e onde mexe

- **DB:** nova tabela + migração simples (idempotente).
- **`scheduler.py`:** filtro adicional em `next_pending_contact` ou imediatamente após.
- **`telegram_bot.py`:** comandos novos + handler do botão inline novo.
- **`csv_utils.py`:** chamada para checar blacklist no momento da importação.
- Estimado: ~250-350 linhas. **S/M.**

#### 1.5 Impacto

Alto. Resolve diretamente "mandei para errado", que é o vetor #1 *no nosso cenário específico*.

---

### Fase 2 — Webhook de status (delivered / read / reply)

Sem isso, blacklist automática é cega. É o que diferencia "número que entregou" de "número que
silenciosamente nunca recebeu".

#### 2.1 Endpoint webhook no bot

Evolution v2 envia eventos para um webhook configurado por instância. Eventos relevantes:

- `messages.upsert` — entrada (resposta do cliente). Aparece também `chatLid`.
- `messages.update` — mudança de status: `sent` → `delivered` → `read`.
- `connection.update` — mudança de estado da conexão. Útil para detectar shadowban / drop.

**Subir um endpoint HTTP** dentro do bot Python. Opções:
- Adicionar `aiohttp.web` server no `main.py` rodando junto com o Telegram poller.
- Caminho mais simples: subir um pequeno serviço FastAPI/aiohttp em paralelo escutando em
  porta interna do Docker (não exposta).

#### 2.2 Tabela `contact_health`

```sql
CREATE TABLE contact_health (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vendor_id INTEGER NOT NULL,
    phone TEXT NOT NULL,
    chat_lid TEXT,
    last_sent_at TIMESTAMP,
    last_delivered_at TIMESTAMP,
    last_read_at TIMESTAMP,
    last_replied_at TIMESTAMP,
    consecutive_no_delivery INTEGER NOT NULL DEFAULT 0,
    last_reply_text TEXT,
    UNIQUE(vendor_id, phone)
);
CREATE INDEX idx_contact_health_phone ON contact_health(phone);
CREATE INDEX idx_contact_health_vendor ON contact_health(vendor_id);
```

#### 2.3 Suporte a `@lid` (mudança de protocolo do WhatsApp)

Z-API documenta: o WhatsApp passou a usar `@lid` (Linked ID) como identificador anônimo em
alguns contextos, especialmente para usuários que ativaram a privacidade do número. **A migração
é gradual e em curso. Ignorar agora vira bug depois.**

Decisões:
- Aceitar `phone` *e* `chatLid` no webhook.
- Salvar ambos em `contact_health` quando ambos vêm. Quando só vem `@lid`, salvar `chat_lid`
  com `phone=NULL`.
- Para envio, manter `phone` quando temos. `@lid`-only só é usável se o destinatário já
  conversou com a gente (o `@lid` veio do webhook de entrada).
- Endpoint `whatsappNumbers` da Evolution devolve o `@lid` correspondente a um telefone — útil
  para inicializar o mapping de toda a base.

#### 2.4 Classificação quente / morno / frio

Derivada de `contact_health`:

| Classe | Critério | Como tratar na campanha |
|---|---|---|
| **Quente** | `last_replied_at < 90d` OR `last_read_at < 30d` | Disparar à vontade |
| **Morno** | `last_delivered_at < 180d` AND não respondeu nos últimos 180d | OK, mas em batch menor |
| **Frio** | `last_delivered_at >= 6 meses` ou nunca confirmou delivery em ≥ 2 campanhas | **Não disparar em massa.** Pré-flight pede confirmação manual |
| **Sem histórico** | Nunca foi contactado neste sistema | Tratar como "morno cauteloso" |

#### 2.5 Pré-flight obrigatório antes de `/disparar`

Substitui o `/disparar` direto. Tela nova no Telegram antes de começar:

```
Lista: Clientes Maio
Total: 320 contatos

  ✓ Quentes (responderam em < 90d): 84
  ~ Mornos (entregam mas não respondem): 152
  ⚠ Frios (sem entrega há > 6m): 47
  ⚠ Sem histórico nesta base: 37
  ✗ Em blacklist: 0 (já filtrados)

[Verificar WhatsApp ativo]  [Excluir frios]  [Continuar]  [Cancelar]
```

#### 2.6 Custo e onde mexe

- **`evolution.py`:** novo método `whatsapp_numbers(numbers: List[str])` para validar.
- **`main.py`:** subir webhook server.
- **Novo `app/webhook.py`:** handlers de `messages.upsert`, `messages.update`, `connection.update`.
- **`db.py`:** tabela e métodos para escrever/ler `contact_health`.
- **`scheduler.py`:** classificação no momento de montar a fila + ler/atualizar health.
- **`telegram_bot.py`:** tela de pré-flight + handlers de botões.
- Estimado: ~600-800 linhas. **M/L.**

#### 2.7 Impacto

Crítico. Sem isto, todas as decisões automáticas (auto-blacklist, classificação, auto-pause)
viram chute.

---

### Fase 3 — Auto opt-out por palavra-chave (LGPD + redução de "report spam")

**Por que junto:** se o cliente respondeu "para", "remove", "não quero mais", a LGPD obriga
processar a saída em até 24h. Mas isso *também* protege a conta — quem foi colocado em
blacklist não pode mais receber, então não vai denunciar.

#### 3.1 Detecção de stop-words

Em cima do webhook `messages.upsert` da Fase 2:

```python
STOP_PATTERN = re.compile(
    r"\b(para|pare|parar|remove|remover|sai|sair|"
    r"n[aã]o\s+quero\s+mais|nao\s+quer\s+mais|"
    r"cancela(?:r)?|stop|opt[\- ]?out|"
    r"descadastr(?:a|ar)|sai(?:r)?\s+da\s+lista)\b",
    re.IGNORECASE,
)
```

Quando bate:
1. `INSERT` em `blacklist` com `reason_code='replied_stop'`, `source='auto'`.
2. Mandar mensagem de confirmação para o cliente (LGPD exige confirmação clara):
   *"Pronto. Você não receberá mais nossas mensagens. Se mudar de ideia, é só responder
   por aqui."* (texto configurável por vendedora).
3. Notificar a vendedora no Telegram: "Cliente X pediu para sair, removi."
4. Se houver campanha *running* daquela vendedora, remover esse contato da fila atual.

#### 3.2 Linha de opt-out na própria campanha

Adicionar configuração por vendedora: appendar opcionalmente "Para parar de receber, responda
PARE" na última caption (a que carrega o texto). Isso **reduz** report-spam (cliente prefere
responder PARE do que clicar em "Denunciar"), o que protege a conta.

Não fazer isso obrigatório — algumas vendedoras vão recusar por desgastar a copy. Deixar
opt-in com flag `add_optout_footer` por vendedora ou por campanha.

#### 3.3 Registro de consentimento (mínimo viável LGPD)

Para o cenário do bot, o que dá pra implementar com pouco esforço:

- Tabela `consent_log` registrando quando a vendedora adicionou o número à lista (timestamp,
  user_id, lista, fonte CSV/VCF/manual).
- Não é "consentimento opt-in formal" — é prova de origem, que é a versão minimamente defensável
  para o caso de a vendedora ser questionada. Para opt-in real precisaria de captura via
  formulário externo, que está fora do escopo deste bot.
- Tabela `optout_log` registrando todo opt-out automático ou manual, mantido por 5 anos
  (LGPD).

#### 3.4 Custo e onde mexe

- **`webhook.py`:** detector de stop-words, ação de blacklist + reply de confirmação.
- **`db.py`:** `consent_log`, `optout_log`, retenção/limpeza.
- **`scheduler.py`:** opção `add_optout_footer` aplicada na última mídia.
- **`telegram_bot.py`:** comando para configurar texto de opt-out e footer.
- Estimado: ~200-300 linhas. **S/M.**

#### 3.5 Impacto

Alto. LGPD compliance + redução de report-spam.

---

### Fase 4 — Variação de conteúdo (anti-fingerprint)

**Por que importa no nosso caso:** vendedora não usa `{nome}`, então hoje cada cliente recebe
texto **byte-a-byte idêntico** + imagem **byte-a-byte idêntica**. Z-API: "trocar variáveis
nominais não é suficiente". WhatsApp tem hashing/fingerprint do conteúdo agregado.

#### 4.1 Recompressão JPEG por destinatário

Em `app/scheduler.py::CampaignScheduler._send_contact`, hoje:

```python
media_base64 = media_cache.get(media["path"])
if media_base64 is None:
    media_base64 = await file_to_base64(media["path"])
    media_cache[media["path"]] = media_base64
```

Vira:

```python
variants = media_cache.get(media["path"])  # List[bytes] de N variantes
if variants is None:
    variants = await build_variants(media["path"], n=8)
    media_cache[media["path"]] = variants
chosen = random.choice(variants)
media_base64 = base64.b64encode(chosen).decode()
```

`build_variants` usa Pillow para:
- Recompressão JPEG com `quality=random.randint(80, 92)`.
- Strip EXIF.
- Opcionalmente: resize ±1-2px aleatório (margem fina o suficiente para não estragar visual).

Geração N=8 variantes na criação da campanha; sorteio uma por destinatário. Visualmente
indistinguível, hash diferente.

**Dependência nova:** `Pillow` (`pip install Pillow`). É leve.

**Custo:** ~80 linhas + dep. **S.**
**Impacto:** alto. Hoje todo cliente recebe a mesma imagem, é o sinal de fingerprint mais óbvio.

#### 4.2 Spintax simples na caption

Aceitar sintaxe `{a|b|c}` na caption. Hoje só substitui `{nome}`. Aceitar também:

```
{Oi|Olá|Bom dia|Boa tarde}, chegaram peças novas...
```

Sorteia uma alternativa por contato. Vendedora não usa `{nome}` por causa dos nomes-caos, mas
saudação é fácil de variar.

Implementação: regex `r"\{([^{}|]+(?:\|[^{}|]+)+)\}"` + `random.choice(opts)`.

**Custo:** ~25 linhas. **S.** **Impacto:** médio.

#### 4.3 Limite de fotos: manter 5, com aviso visual no Telegram

Discussão original: "talvez limitar em 3". Do ponto de vista de **ban**, irrelevante — o que pesa
é o padrão "sempre exatamente N fotos toda campanha". Manter 5 e simplesmente deixar a
vendedora variar naturalmente entre 2-4 conforme monta dá padrão menos rígido. Do ponto de vista
de **engajamento e banda na VPS**, 3-4 é melhor; deixe a UX no Telegram sugerir, sem forçar.

---

### Fase 5 — Defesa contra shadowban e auto-pause

#### 5.1 Detecção

Strings específicas que a Evolution propaga vindas do Baileys/WhatsApp Web:

- `"likely shadow ban"`
- `"Whatsapp rejected sending this message"`
- `"connection closed"` (em sequência rápida com sucesso na API)
- HTTP 4xx do tipo "not authorized", "blocked"

Em `evolution.py::_request`, detectar e expor flag específica em vez de só lançar
`EvolutionError` genérico.

#### 5.2 Contador de "estranhezas consecutivas"

Em `_run_campaign`:

```python
suspicion = 0
SUSPICION_LIMIT = 3
SUSPICION_RESET_AFTER_OK = 5  # 5 envios OK seguidos zera o contador
```

A cada envio:
- Sucesso confirmado → `suspicion = max(0, suspicion - 1)` (decay).
- Erro genérico → `suspicion += 1`.
- Erro de shadowban / connection drop logo após sucesso de API → `suspicion += 2`.
- Quando `suspicion >= SUSPICION_LIMIT`: **auto-pause**, mensagem para a vendedora no Telegram:
  *"Conta com sintomas de restrição. Recomendo: NÃO desconectar nem recriar instância. Aguardar
  algumas horas, abrir o WhatsApp Web no celular e enviar mensagens manualmente. Quando voltar
  ao normal, retomar pelo Telegram."*

#### 5.3 Não recriar instância automaticamente

**Já está correto** no código: `ensure_fresh_qr` se recusa a apagar/recriar instância existente.
Z-API documenta explicitamente que recriar piora o shadowban. Manter como está.

#### 5.4 Custo e onde mexe

- **`evolution.py`:** classificação de erro.
- **`scheduler.py`:** contador + auto-pause.
- **`telegram_bot.py`:** mensagem instrutiva para a vendedora.
- Estimado: ~120 linhas. **S.**

#### 5.5 Impacto

Médio-alto. Salvar uma noite quando o número entra em zona de risco evita que vire ban
definitivo.

---

### Fase 6 — Cota diária por número, não por campanha

Hoje `MAX_*_CLIENTS_PER_CAMPAIGN` é por **campanha**. Se a vendedora dispara duas campanhas
de 200 no mesmo dia, manda 400. Wasenderapi 2026: número warmed aguenta 80-200/dia; novo,
20-50/dia.

#### 6.1 Tabela de cota

```sql
CREATE TABLE daily_send_quota (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vendor_id INTEGER NOT NULL,
    instance_name TEXT NOT NULL,
    quota_date DATE NOT NULL,
    sent_count INTEGER NOT NULL DEFAULT 0,
    UNIQUE(vendor_id, instance_name, quota_date)
);
```

#### 6.2 Configuração

`.env`:

```env
DAILY_CAP_HOT=300
DAILY_CAP_WARM=120
DAILY_CAP_COLD=20
DAILY_CAP_NEW_NUMBER=40
```

`new_number=true` por instância criada nos últimos N dias (campo em `vendors` ou tabela
auxiliar).

#### 6.3 Comportamento

Antes de cada envio, checar `sent_count` do dia. Se passou do cap:
1. Pausar a campanha (ou marcar como "aguardando manhã").
2. Notificar a vendedora: "Cota diária atingida. Retomo amanhã 09:00."
3. No próximo dia (cron interno simples ou checagem ao reabrir), retomar.

#### 6.4 Custo e onde mexe

- **`db.py`:** nova tabela, increment, leitura.
- **`scheduler.py`:** check antes de cada envio + handling de "esperar até amanhã".
- **`config.py`:** novos envs.
- Estimado: ~150 linhas. **S/M.**

#### 6.5 Impacto

Alto contra a "campanha gigante num dia só".

---

### Fase 7 — Validação `whatsappNumbers` antes da campanha

Evolution v2 expõe `POST /chat/whatsappNumbers/{instance}` recebendo lista de telefones e
respondendo quais existem no WhatsApp.

- Não pega reciclados (número existe, com outro dono).
- **Pega**: linhas desativadas, contas deletadas, erro de digitação.
- Pega também o `@lid` correspondente — útil para popular o mapping inicial de
  `contact_health`.

#### Implementação

- Botão "Verificar WhatsApp ativo" no pré-flight da Fase 2.6.
- Marca os inválidos como `failed` antes do início da campanha, com motivo `"sem whatsapp"`.
- Cache do resultado em `contact_health` (ou tabela própria) para não revalidar 320 contatos
  toda campanha.

**Custo:** ~80 linhas. **S.**
**Impacto:** baixo-médio. Mata o ruído fácil. Não é o filtro principal.

---

### Fase 8 — Presença "digitando..." (impacto incremental)

Evolution v2 expõe `POST /chat/sendPresence/{instance}` com `{"number": ..., "presence": "composing", "delay": <ms>}`.

#### Quando usar

Antes do envio, especialmente quando há texto. Delay proporcional ao tamanho do texto:
~40ms por caractere, com piso de 1.5s e teto de 8s.

#### Custo e onde mexe

- **`evolution.py`:** método `send_presence`.
- **`scheduler.py::_send_contact`:** chamada antes do envio.
- Estimado: ~40 linhas. **S.**

#### Impacto

Baixo-médio. Z-API lista entre os pilares de "simulate human behavior" (#7), mas não está no
top dos fatores. Vale fazer porque é barato.

---

### Fase 9 — Order randomization e jitter pós-pausa

Hoje a fila é processada em ordem do `INSERT` em `campaign_contacts`. DDDs vizinhos saem em
sequência, o que correlaciona com tipos de banimento por blocos.

- **Embaralhar a fila** após criar a campanha (`ORDER BY RANDOM()` no SELECT inicial, ou
  `random.shuffle` em memória ao montar).
- Após uma pausa longa (`pause_every`), introduzir um *jitter* extra de 5-30s antes do primeiro
  envio do próximo bloco — não retomar exatamente em ritmo.

**Custo:** ~30 linhas. **S.** **Impacto:** baixo. Vale por barato.

---

### Fase 10 — Itens de longo prazo / arquiteturais

Não fazer agora, mas registrar.

- **Round-robin entre múltiplos números da mesma vendedora.** Diluir 400 envios em 2
  números é radicalmente mais seguro do que num só. Requer mudança no modelo de campanha:
  hoje `instance_name` é por vendedora. Migrar para *lista de instâncias ativas* por
  vendedora, com balanceamento.
- **Bucket "arquivado" automático.** Contato sem nenhum sinal positivo (delivery + read +
  reply) há ≥ 12 meses sai da lista padrão e exige reconfirmação manual antes de voltar.
  Defesa contra reciclagem em massa de chips antigos.
- **Tier system semelhante ao da Meta.** Começar nova vendedora em "tier 1" (40/dia), subir
  para "tier 2" (120/dia) após 7 dias sem incidente, "tier 3" (250/dia) após 30 dias.
  Automatiza o warmup.
- **Página simples de opt-in público.** URL com formulário onde o cliente confirma "quero
  receber novidades". Output: token de consentimento que a vendedora cola junto com o
  contato. Eleva o nível de defesa LGPD do "prova de origem" para "consentimento auditável".
  Fora do escopo deste bot, mas vale como projeto separado.

---

## Tabela mestre de prioridades

| Ordem | Fase | Item | Custo | Impacto |
|---|---|---|---|---|
| 1 | 0 | Operacional: env, perfil de WhatsApp, comportamento humano, 2-step | zero | alto |
| 2 | 1 | Blacklist com 3 camadas + import + comandos Telegram | S/M | alto |
| 3 | 2 | Webhook + `contact_health` + classificação + pré-flight + `@lid` | M/L | crítico (destrava 3, 5) |
| 4 | 3 | Auto opt-out por palavra-chave + LGPD logs + footer opcional | S/M | alto |
| 5 | 4.1 | Recompressão JPEG por destinatário | S | alto (no nosso conteúdo idêntico) |
| 6 | 5 | Detecção de shadowban + auto-pause + decay | S | médio-alto |
| 7 | 6 | Cota diária por número | S/M | alto |
| 8 | 4.2 | Spintax na saudação | S | médio |
| 9 | 7 | Validação `whatsappNumbers` no pré-flight | S | baixo-médio |
| 10 | 8 | Presença "digitando..." | S | baixo-médio |
| 11 | 9 | Embaralhar fila + jitter pós-pausa | S | baixo |
| 12 | 10 | Round-robin de instâncias / tier system / arquivamento / opt-in público | L | alto a longo prazo |

---

## Decididos a NÃO fazer (e por quê)

| Item | Razão |
|---|---|
| Trocar VPS para BR | Z-API testou rotação de IP em ciclos e não viu redução significativa. IP/ASN é fator secundário. Custo (migração + downtime) > benefício. Reabrir só se evidência empírica nossa mudar |
| Forjar User-Agent / fingerprint do Baileys | Detecção é por padrão de envio (servidor), não por client string |
| Tornar opt-out footer obrigatório em toda campanha | Algumas vendedoras vão recusar por desgastar a copy. Manter opt-in. LGPD vê o comportamento de processamento, não exige texto exato em cada msg |
| Pairing code em vez de QR | Indiferente para risco de ban |
| Desabilitar/reativar instância automaticamente em sintoma de problema | Z-API explicitamente recomenda **não** recriar — piora |
| Logout/login programado para "renovar sessão" | Wasenderapi confirma: piora |

---

## Como medir resultado

Sem métrica, não dá para saber se o plano funcionou.

- **Tempo de vida das contas** (dias entre conexão inicial e ban/restrição). Antes/depois.
- **Taxa `delivered/sent`** por instância, semanal. Queda súbita = sinal de shadowban.
- **Taxa `replied/sent`** por instância, semanal. Subir = warmup funcionando.
- **Auto-blacklist por mês** (quantos `replied_stop` + `wrong_person`). Crescimento estável é
  saúde; explosão num mês = sintoma de lista podre concentrada.
- **Suspicion auto-pauses por mês.** Se nunca acionar, é falso senso de segurança *ou* vendedoras
  bem comportadas. Se acionar muito, ajustar limites.

Tudo computável a partir de `contact_health` + `blacklist` + logs do bot.

---

## Apêndice A — Stop-words sugeridas (PT-BR)

Regex inicial, ajustar conforme campo:

```
\b(
  para|pare|parar|
  remove|remover|removam|
  sai|sair|saiam|
  n[aã]o\s+quero\s+(mais|receber)|
  cancela(?:r|m|do)?|
  stop|opt[\- ]?out|
  descadastr(?:a|ar|e|em|amento)|
  excluir\s+(meu\s+)?(numero|número|contato)|
  (sai|tirar)\s+da\s+lista|
  perd[eê]u\s+meu\s+tempo|
  spam
)\b
```

Tratar como sugestão, não detecção 100%. Vendedoras podem revisar manualmente as detecções
no comando `/optout_log` (ler últimos 30 auto-opt-outs).

---

## Apêndice B — Variáveis novas em `.env`

```env
# Janela horária e perfil
SEND_WINDOW=09:00-19:00
DEFAULT_PROFILE=humano_100

# Cota diária por número
DAILY_CAP_HOT=300
DAILY_CAP_WARM=120
DAILY_CAP_COLD=20
DAILY_CAP_NEW_NUMBER=40
DAILY_CAP_NEW_NUMBER_DAYS=14   # quantos dias um chip é considerado "novo"

# Webhook
WEBHOOK_LISTEN_HOST=0.0.0.0
WEBHOOK_LISTEN_PORT=8090
WEBHOOK_SHARED_TOKEN=...        # autenticação básica entre Evolution e bot

# Variação de conteúdo
IMAGE_VARIANTS_PER_CAMPAIGN=8
IMAGE_VARIANT_QUALITY_MIN=80
IMAGE_VARIANT_QUALITY_MAX=92

# Shadowban
SUSPICION_LIMIT=3
SUSPICION_DECAY_OK=1
SUSPICION_BUMP_SHADOWBAN=2
```

---

## Apêndice C — Comandos novos no Telegram

| Comando | Função |
|---|---|
| `/blacklist <numero> [motivo]` | Adiciona à blacklist da vendedora |
| `/blacklist_arquivo` | Importar lista (CSV) para blacklist |
| `/blacklist_remover <numero>` | Remove da blacklist |
| `/blacklist_listar` | Listagem paginada |
| `/optout_log` | Últimos 30 auto-opt-outs detectados |
| `/optout_texto <texto>` | Configura mensagem de confirmação enviada após opt-out |
| `/optout_footer on/off` | Liga/desliga "Para parar, responda PARE" no fim da última caption |
| `/saude_lista <lista>` | Mostra distribuição quente/morno/frio/blacklist da lista |
| `/cota_hoje` | Mostra envios contabilizados hoje vs cap |

Botões inline novos no painel de progresso da campanha:
- **"Adicionar último à blacklist"** — adiciona o último contato disparado
- **"Pular frios restantes"** — finaliza só com quentes/mornos quando o usuário decide

---

## Próximo passo recomendado

Executar **Fase 0 hoje** (`.env` + comportamento humano dos números + 2-step). É grátis e dá ganho.

Em paralelo, decidir se vamos atacar **Fase 1 (blacklist) sozinha primeiro** ou se faz mais
sentido implementar **Fase 2 (webhook) antes**, porque a blacklist fica muito mais poderosa
quando tem o webhook a alimentando com sinais automáticos.

Recomendação: **Fase 1 primeiro**, mesmo sem webhook. Defesa imediata contra "mandei para
errado" via comandos manuais e import. Depois Fase 2 destrava as automatizações em cima.
