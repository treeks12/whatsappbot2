// Patch de SEGURANCA e CORRECAO: pairing code estavel na Evolution 2.4.0-rc2.
//
// Causa-raiz (vista no codigo compilado de /evolution/dist/main.js):
// a cada ciclo de QR (~45s), o handler de QR executa
//     requestPairingCode(this.phoneNumber)
// sem codigo custom. O Baileys entao GERA UM CODIGO NOVO e reenvia
// "link_code_companion_reg" (stage companion_hello). Cada novo registro
// SUBSTITUI o pendente no servidor do WhatsApp: o codigo que a vendedora
// copiou morre em segundos -> "codigo incorreto" ou "confira o numero".
//
// Correcao: passar o codigo ja gerado como customPairingCode.
// - 1o ciclo: gera codigo forte (pelo proprio baileys, randomBytes).
// - ciclos seguintes: RE-REGISTRAM O MESMO codigo, mantendo a sessao
//   pendente viva e o codigo identico durante toda a janela de 3 minutos.
//
// O patch aborta o build (exit 1) se o alvo nao existir OU aparecer mais de
// uma vez: se a imagem base mudar, preferimos build quebrado a patch cego.
'use strict';

const fs = require('fs');

const PATH = '/evolution/dist/main.js';
const OLD =
  'this.instance.qrcode.pairingCode=await this.client.requestPairingCode(this.phoneNumber)';
const NEW =
  'this.instance.qrcode.pairingCode=await this.client.requestPairingCode(this.phoneNumber,this.instance.qrcode.pairingCode)';

const src = fs.readFileSync(PATH, 'utf8');
const count = src.split(OLD).length - 1;

if (count !== 1) {
  console.error(`[patch-pairing] ABORTANDO: esperava 1 ocorrencia do alvo, encontrei ${count}`);
  process.exit(1);
}

fs.writeFileSync(PATH, src.replace(OLD, NEW));
console.log('[patch-pairing] OK: pairingCode estavel aplicado em', PATH);