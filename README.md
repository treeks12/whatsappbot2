# WhatsApp Bot v2

Bot Telegram para operar campanhas pequenas de WhatsApp usando Evolution API 2.4+.

## O Que Ele Faz

- Cada vendedora usa o proprio usuario Telegram autorizado.
- Cada vendedora conecta um WhatsApp proprio via `/login`.
- Cada vendedora ganha uma instancia Evolution propria: `vendor_<telegram_user_id>`.
- Campanhas de vendedoras diferentes rodam em paralelo.
- Dentro do mesmo numero, o envio e sequencial e cadenciado.
- Aceita contatos em CSV, VCF bruto, ZIP com CSV/VCF, ou contatos encaminhados pelo Telegram.
- Aceita imagem com legenda, varias imagens com legenda na ultima, texto puro, ou somente imagem.

## Comandos

- `/start`: mostra o menu basico.
- `/login`: conecta o WhatsApp da vendedora via QR Code.
- `/conexao`: mostra o estado atual da conexao WhatsApp da vendedora.
- `/desconectar`: tenta desconectar a sessao sem logout; se a Evolution nao suportar, desliga o container mantendo a sessao.
- `/nova`: cria campanha no perfil de precaucao.
- `/nova_precaucao`: cria campanha mais cuidadosa.
- `/nova_confianca`: cria campanha para clientes habituais/de confianca.
- `/listas`: cria, incrementa, exporta, reduz e restaura listas de contatos da vendedora.
- `/pronto`: confirma a etapa atual, como terminar contatos ou terminar imagens.
- `/sem_midia`: pula imagens e vai direto para texto.
- `/sem_texto`: depois das imagens, deixa a campanha pronta sem legenda/texto.
- `/disparar`: inicia a campanha pronta.
- `/status`: mostra ultimas campanhas e progresso.
- `/cancelar`: cancela a campanha ativa da vendedora.
- `/blacklist <numero> [motivo]`: bloqueia o telefone; ele nunca mais entra em listas nem campanhas.
- `/blacklist_remover <numero>`: tira o telefone da blacklist.
- `/blacklist_listar`: mostra os telefones bloqueados, com botoes para paginar.
- `/blacklist_arquivo`: importa varios telefones de uma vez via .csv, .vcf, .zip ou contatos do Telegram.

Durante o disparo, a mensagem de progresso tem botoes inline para pausar, retomar, cancelar com confirmacao e adicionar o ultimo destinatario a blacklist.

## Ordem Da Fila

Os contatos importados mantem `row_index` para auditoria, mas a ordem de envio usa `dispatch_order`. Essa ordem e calculada uma vez por campanha e intercala DDDs/grupos de telefone, evitando que listas importadas com DDDs vizinhos saiam em blocos longos. A ordem fica salva no SQLite, entao reiniciar o bot nao embaralha a campanha de novo.

## Pre-flight Antes Do Disparo

`/disparar` agora abre um painel de pre-flight em vez de comecar imediatamente. O bot classifica os contatos pendentes em quatro grupos com base no historico de eventos (delivered/read/replied) capturados pelo webhook da Evolution:

- **Quentes**: responderam em < 90 dias OU leram em < 30 dias.
- **Mornos**: tem ao menos uma entrega confirmada e nao se enquadram em quente/frio.
- **Frios**: ultima entrega ha mais de 180 dias OU 2+ envios seguidos sem nenhuma entrega.
- **Sem historico**: nunca foram tocados nessa base.

Botoes do painel:

- **Disparar agora**: inicia a campanha como antes.
- **Excluir frios**: marca os contatos frios como falha (`preflight: frio`) sem enviar.
- **Verificar WhatsApp ativo**: chama `POST /chat/whatsappNumbers` na Evolution e marca como falha (`preflight: sem whatsapp`) os numeros que nao existem mais.
- **Cancelar disparo**: fecha o painel sem mexer na campanha.

Nas duas primeiras execucoes, "Frios" e "Sem historico" podem aparecer iguais (ambos sem dados); a precisao melhora a medida que os webhooks de delivered/read/replied chegam para campanhas seguintes.

## Webhook Da Evolution

Para alimentar o `contact_health` que sustenta o pre-flight (e mais adiante a auto-blacklist por palavra-chave de cancelamento), o bot sobe um pequeno servidor HTTP em paralelo com o poller do Telegram, dentro do mesmo container. A Evolution e configurada para chamar essa URL por instancia.

Configuracao no `.env`:

```env
WEBHOOK_LISTEN_HOST=0.0.0.0
WEBHOOK_LISTEN_PORT=8090
WEBHOOK_TOKEN=
WEBHOOK_PUBLIC_URL=
WEBHOOK_AUTO_CONFIGURE=true
```

- `WEBHOOK_PUBLIC_URL` e a URL que a Evolution chama. Como bot e Evolution rodam na mesma rede `bot-net` do Docker, o nome do container resolve internamente. **A porta nao precisa ser exposta para fora da VPS**.
- Se `WEBHOOK_TOKEN` e `WEBHOOK_PUBLIC_URL` ficarem vazios, o bot gera um token estavel a partir da `EVOLUTION_API_KEY` e usa `http://whatsapp-bot-v2:8090/webhook/<token>`.
- `WEBHOOK_TOKEN` aparece tanto na URL quanto na rota. Vale como camada extra de auth alem do isolamento da rede.
- `WEBHOOK_AUTO_CONFIGURE=true` faz o bot tentar reconfigurar webhooks no boot se a Evolution ja estiver pronta, e sempre que a vendedora abre/conecta sessao. Idempotente.
- O webhook escuta os eventos `MESSAGES_UPSERT` (resposta do cliente), `MESSAGES_UPDATE` (delivered/read), `CONNECTION_UPDATE` (logado para diagnostico) e `SEND_MESSAGE` (confirmacao do envio).

## Perfis

### Precaucao

Use com listas que exigem mais cuidado.

- Clientes novos: 20 a 50s entre contatos.
- Clientes ja conhecidos pelo historico do bot: 10 a 25s.
- Entre fotos da mesma pessoa: 2 a 6s.
- Pausa maior: a cada 30 contatos, por 120 a 300s.
- Limite padrao: sem limite fixo (`0` no `.env`). Use `MAX_PRECAUTION_CLIENTS_PER_CAMPAIGN` se quiser limitar.

### Confianca

Use apenas para clientes habituais, que reconhecem o numero/loja e com quem ja existe relacao.

- Clientes novos: 8 a 20s entre contatos.
- Clientes ja conhecidos pelo historico do bot: 5 a 12s.
- Entre fotos da mesma pessoa: 1 a 3s.
- Pausa maior: a cada 75 contatos, por 60 a 120s.
- Limite padrao: sem limite fixo (`0` no `.env`). Use `MAX_TRUSTED_CLIENTS_PER_CAMPAIGN` se quiser limitar.

## Arquivo `.env`

Este backup privado inclui o `.env` atual de uso local. Para outra maquina, revise principalmente:

```env
TELEGRAM_BOT_TOKEN=...
TELEGRAM_ADMIN_IDS=...
EVOLUTION_API_URL=...
EVOLUTION_API_KEY=...
MAX_TRUSTED_CLIENTS_PER_CAMPAIGN=0
MAX_PRECAUTION_CLIENTS_PER_CAMPAIGN=0
MAX_MEDIA_FILE_MB=3
MAX_PARALLEL_MEDIA_UPLOADS=2
PROGRESS_UPDATE_INTERVAL_SECONDS=5
CLEANUP_CAMPAIGN_FILES_ON_FINISH=true
CONTACT_LIST_SNAPSHOT_KEEP=3
CONTACT_LIST_SNAPSHOT_DAYS=14
DEFAULT_PROFILE=precaucao_100
SEND_WINDOW=
```

Na maquina local atual, `EVOLUTION_API_URL=http://host.docker.internal:8081`.

Em uma VPS, troque para o endereco real da Evolution. Exemplos:

```env
EVOLUTION_API_URL=http://evolution-api:8080
```

ou:

```env
EVOLUTION_API_URL=https://evolution.seudominio.com
```

## Subir O Bot

Com Docker:

```powershell
docker compose up -d --build
```

Ver logs:

```powershell
docker compose logs -f bot-v2
```

Reiniciar apos alterar `.env`:

```powershell
docker compose up -d --force-recreate bot-v2
```

Na VPS, use o compose especifico:

```bash
docker compose -f docker-compose.vps.yml logs -f bot
docker compose -f docker-compose.vps.yml up -d --build bot
```

Smoke test da Evolution:

```powershell
docker exec -w /app whatsapp-bot-v2 python -m app.smoke_evolution
```

Smoke test das listas:

```powershell
docker exec -w /app whatsapp-bot-v2 python -m app.smoke_contact_lists
```

## Fluxo De Uso

1. No Telegram, use `/login`.
2. Escaneie o QR Code no WhatsApp da vendedora.
3. Crie uma campanha:

```text
/nova_precaucao
```

ou:

```text
/nova_confianca
```

4. Escolha uma lista salva, crie uma nova lista, ou carregue contatos so para a campanha.
5. Use `/pronto` se estiver enviando contatos soltos pelo Telegram.
6. Envie imagens, ou use `/sem_midia`.
7. Use `/pronto`.
8. Envie legenda/texto, ou use `/sem_texto`.
9. Use `/disparar`.

## Formatos De Contatos

CSV aceito:

```csv
nome,telefone
Cliente,5511999999999
```

Colunas de telefone aceitas:

```text
telefone, phone, numero, número, celular, whatsapp
```

Colunas de nome aceitas:

```text
nome, name, cliente
```

VCF de iPhone/Android tambem e aceito. Se o Telegram transformar o VCF em cartoes de contato e comer DDD, envie o VCF dentro de um ZIP.

## Listas De Contatos

Cada vendedora tem as proprias listas. No fluxo `/nova`, `/nova_confianca` ou `/nova_precaucao`, o bot oferece usar lista salva, criar lista nova, adicionar contatos a uma lista existente, ou carregar contatos apenas para aquela campanha.

O telefone normalizado e a chave de duplicidade. Nome repetido nao bloqueia importacao. Se o telefone ja existir, o bot ignora o contato; ele so atualiza o nome quando o nome antigo era generico como `Cliente`.

O menu `/listas` permite:

- criar lista;
- adicionar contatos por CSV, VCF, ZIP ou contatos do Telegram;
- exportar CSV limpo;
- reduzir lista enviando arquivo/contatos a remover;
- renomear;
- restaurar backups;
- apagar lista.

Antes de reduzir ou restaurar uma lista, o bot cria backup automatico. Por padrao, guarda ate 3 backups por lista por ate 14 dias, evitando acumular lixo.

## VPS Pequena

Para VPS com pouca RAM:

```env
MAX_PARALLEL_MEDIA_UPLOADS=1
MAX_MEDIA_FILE_MB=1
```

Para VPS mais tranquila, como 8 GB RAM / 4 vCPU:

```env
MAX_PARALLEL_MEDIA_UPLOADS=2
MAX_MEDIA_FILE_MB=3
```

Evite fazer deploy/build pesado do ecommerce ao mesmo tempo que uma campanha grande com imagens.

## Evolution Ligada Somente Quando Precisa

No deploy VPS, o bot pode controlar o container `evolution-api` pelo Docker socket:

```env
EVOLUTION_DOCKER_CONTROL=true
EVOLUTION_DOCKER_CONTAINER=evolution-api
DOCKER_SOCKET_PATH=/var/run/docker.sock
EVOLUTION_IDLE_STOP_SECONDS=600
```

Com isso, a regra fica:

- Sem campanha em disparo (`running` ou `paused`): o bot pode desligar o container da Evolution.
- `/login` e `/nova`: o bot liga a Evolution para gerar QR ou validar conexao. Se a conexao ja estiver aberta e nao houver disparo, desliga de novo.
- QR pendente: se ninguem usar, o bot tenta desligar a Evolution depois de `EVOLUTION_IDLE_STOP_SECONDS`.
- `/disparar`: o bot liga a Evolution e mantem ligada ate concluir, falhar ou cancelar.
- `/desconectar`: primeiro tenta endpoint seguro de `disconnect`; se esse build da Evolution nao tiver o endpoint, desliga o container.

Isso nao apaga instancia e nao faz logout. As sessoes ficam nos volumes `evolution_store`, `evolution_instances` e no banco Postgres da Evolution. O unico ponto sensivel e que montar `/var/run/docker.sock` da poder de Docker ao container do bot, entao mantenha esse projeto privado e sem comandos de usuario livre.

O fluxo de QR tambem evita apagar/recriar instancia automaticamente. Se a Evolution nao devolver QR para uma instancia existente, o bot mostra erro em vez de destruir a sessao salva.

## Dados Locais

- Banco SQLite: `data/bot.sqlite3`
- Midias de campanha: `campaigns/`

Essas pastas ficam fora do Git por padrao.

Quando `CLEANUP_CAMPAIGN_FILES_ON_FINISH=true`, arquivos de campanha sao removidos depois que a campanha conclui, falha ou e cancelada. O historico util continua no SQLite, incluindo telefones enviados para identificar clientes conhecidos em campanhas futuras.

Se o bot reiniciar no meio de uma campanha, campanhas que estavam `running` ou `paused` sao marcadas como `failed` no proximo boot para nao ficarem bloqueando a vendedora para sempre.

## Observacoes

O bot envia exatamente a legenda informada. Ele nao adiciona rodape, disclaimer ou texto automatico para o cliente.

Evolution/Baileys e um caminho nao oficial em relacao ao WhatsApp. Use primeiro com escopo pequeno, especialmente quando trocar VPS, dominio, numero ou versao da Evolution.
