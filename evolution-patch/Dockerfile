# Imagem derivada da Evolution API (MESMA versao em producao: 2.4.0-rc2)
# com um unico patch: pairingCode ESTAVEL durante toda a tentativa de conexao.
# Sem o patch, a cada ciclo de QR a Evolution gera codigo novo e cada
# companheiro "companion_hello" cancela o pendente no servidor do WhatsApp
# -> "codigo incorreto" / "confira o numero".
FROM evoapicloud/evolution-api:2.4.0-rc2

COPY patch-pairing-stable-code.js /tmp/patch-pairing-stable-code.js
RUN node /tmp/patch-pairing-stable-code.js && rm /tmp/patch-pairing-stable-code.js