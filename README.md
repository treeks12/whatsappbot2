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
- `/desconectar`: explica por que o bot nao usa logout automatico como desconexao.
- `/nova`: cria campanha no perfil de precaucao.
- `/nova_precaucao`: cria campanha mais cuidadosa, limite padrao de 100 contatos.
- `/nova_confianca`: cria campanha para clientes habituais/de confianca, limite padrao de 300 contatos.
- `/pronto`: confirma a etapa atual, como terminar contatos ou terminar imagens.
- `/sem_midia`: pula imagens e vai direto para texto.
- `/sem_texto`: depois das imagens, deixa a campanha pronta sem legenda/texto.
- `/disparar`: inicia a campanha pronta.
- `/status`: mostra ultimas campanhas e progresso.
- `/cancelar`: cancela a campanha ativa da vendedora.

Durante o disparo, a mensagem de progresso tem botoes inline para pausar, retomar e cancelar com confirmacao.

## Perfis

### Precaucao

Use com listas que exigem mais cuidado.

- Clientes novos: 20 a 50s entre contatos.
- Clientes ja conhecidos pelo historico do bot: 10 a 25s.
- Entre fotos da mesma pessoa: 2 a 6s.
- Pausa maior: a cada 30 contatos, por 120 a 300s.
- Limite padrao: 100 contatos.

### Confianca

Use apenas para clientes habituais, que reconhecem o numero/loja e com quem ja existe relacao.

- Clientes novos: 8 a 20s entre contatos.
- Clientes ja conhecidos pelo historico do bot: 5 a 12s.
- Entre fotos da mesma pessoa: 1 a 3s.
- Pausa maior: a cada 75 contatos, por 60 a 120s.
- Limite padrao: 300 contatos.

## Arquivo `.env`

Este backup privado inclui o `.env` atual de uso local. Para outra maquina, revise principalmente:

```env
TELEGRAM_BOT_TOKEN=...
TELEGRAM_ADMIN_IDS=...
EVOLUTION_API_URL=...
EVOLUTION_API_KEY=...
MAX_TRUSTED_CLIENTS_PER_CAMPAIGN=300
MAX_PRECAUTION_CLIENTS_PER_CAMPAIGN=100
MAX_MEDIA_FILE_MB=3
MAX_PARALLEL_MEDIA_UPLOADS=2
PROGRESS_UPDATE_INTERVAL_SECONDS=5
CLEANUP_CAMPAIGN_FILES_ON_FINISH=true
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

Smoke test da Evolution:

```powershell
docker exec -w /app whatsapp-bot-v2 python -m app.smoke_evolution
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

4. Envie CSV, VCF bruto ou ZIP com CSV/VCF.
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

## Dados Locais

- Banco SQLite: `data/bot.sqlite3`
- Midias de campanha: `campaigns/`

Essas pastas ficam fora do Git por padrao.

Quando `CLEANUP_CAMPAIGN_FILES_ON_FINISH=true`, arquivos de campanha sao removidos depois que a campanha conclui, falha ou e cancelada. O historico util continua no SQLite, incluindo telefones enviados para identificar clientes conhecidos em campanhas futuras.

Se o bot reiniciar no meio de uma campanha, campanhas que estavam `running` ou `paused` sao marcadas como `failed` no proximo boot para nao ficarem bloqueando a vendedora para sempre.

## Observacoes

O bot envia exatamente a legenda informada. Ele nao adiciona rodape, disclaimer ou texto automatico para o cliente.

Evolution/Baileys e um caminho nao oficial em relacao ao WhatsApp. Use primeiro com escopo pequeno, especialmente quando trocar VPS, dominio, numero ou versao da Evolution.
