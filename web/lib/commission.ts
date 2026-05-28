/**
 * lib/commission.ts — Fuente unica de verdad para el calculo de comision.
 *
 * Modelo de negocio:
 *  - El cliente deposita un capital fijo (capital_inicial).
 *  - El bot opera con stakes fijos; el capital del cliente es un colchon.
 *  - Comision del 20% sobre cada GANANCIA INDIVIDUAL, pero SOLO sobre la
 *    porcion que mantenga el balance POR ENCIMA del capital_inicial.
 *  - Si el balance esta bajo el capital_inicial -> modo recuperacion: 0% comision.
 *  - Las perdidas nunca pagan comision.
 *
 * IMPORTANTE: Esta funcion debe usarse en cliente y admin sin excepcion.
 *             No reimplementar la logica en otro lugar.
 */

export interface CommissionResult {
  gananciaPositicion: number;     // PnL bruto de esta posicion (puede ser negativo)
  enModoRecuperacion: boolean;    // true si el balance PREVIO estaba bajo capital_inicial
  comisionAdmin: number;          // lo que cobra el admin (>= 0)
  gananciaCliente: number;        // lo que recibe el cliente (puede ser negativo en perdidas)
  balancePrevio: number;
  balanceNuevo: number;
  capitalInicial: number;
}

export interface EstadoCuenta {
  capitalInicial: number;
  balanceActual: number;
  gananciaNeta: number;
  enModoRecuperacion: boolean;
  rendimientoPct: number;
  comisionTotalCobrada: number;
  mensajeEstado: string;
}

const COMMISSION_RATE = 0.20;

/**
 * Calcula la comision de UNA posicion cerrada.
 * Reglas exactas (alineadas con la especificacion):
 *  - pnl <= 0 -> sin comision.
 *  - balance_previo >= capital_inicial -> 20% sobre toda la ganancia.
 *  - balance_nuevo <= capital_inicial -> recuperacion total, sin comision.
 *  - cruce: solo la porcion sobre capital_inicial paga 20%.
 */
export function calcularComisionPosicion(
  capitalInicial: number,
  balancePrevio: number,
  pnlPosicion: number,
): CommissionResult {
  const balanceNuevo = balancePrevio + pnlPosicion;

  // 1) Perdidas: nunca pagan comision.
  if (pnlPosicion <= 0) {
    return {
      gananciaPositicion: pnlPosicion,
      enModoRecuperacion: balancePrevio < capitalInicial,
      comisionAdmin: 0,
      gananciaCliente: pnlPosicion,
      balancePrevio,
      balanceNuevo,
      capitalInicial,
    };
  }

  // 2) Sobre el umbral previamente -> cobrar 20% sobre TODA la ganancia.
  if (balancePrevio >= capitalInicial) {
    const comisionAdmin = pnlPosicion * COMMISSION_RATE;
    return {
      gananciaPositicion: pnlPosicion,
      enModoRecuperacion: false,
      comisionAdmin,
      gananciaCliente: pnlPosicion - comisionAdmin,
      balancePrevio,
      balanceNuevo,
      capitalInicial,
    };
  }

  // 3) En modo recuperacion. ¿Se queda bajo el umbral?
  if (balanceNuevo <= capitalInicial) {
    return {
      gananciaPositicion: pnlPosicion,
      enModoRecuperacion: true,
      comisionAdmin: 0,
      gananciaCliente: pnlPosicion,
      balancePrevio,
      balanceNuevo,
      capitalInicial,
    };
  }

  // 4) Cruza el umbral. Solo la porcion sobre capital_inicial paga 20%.
  const parteRecuperacion = capitalInicial - balancePrevio; // sin comision
  const parteGanancia     = balanceNuevo - capitalInicial;  // con comision
  const comisionAdmin     = parteGanancia * COMMISSION_RATE;

  return {
    gananciaPositicion: pnlPosicion,
    enModoRecuperacion: true,
    comisionAdmin,
    gananciaCliente: parteRecuperacion + (parteGanancia - comisionAdmin),
    balancePrevio,
    balanceNuevo,
    capitalInicial,
  };
}

/**
 * Estado actual de la cuenta del cliente — derivado puro, sin efectos.
 */
export function calcularEstadoCuenta(
  capitalInicial: number,
  balanceActual: number,
  comisionTotalCobrada: number,
): EstadoCuenta {
  const gananciaNeta = balanceActual - capitalInicial;
  const enModoRecuperacion = balanceActual < capitalInicial;
  const rendimientoPct = capitalInicial > 0
    ? (gananciaNeta / capitalInicial) * 100
    : 0;

  const mensajeEstado = enModoRecuperacion
    ? `En recuperacion — faltan $${(capitalInicial - balanceActual).toFixed(2)} para retomar comisiones`
    : `Cuenta en ganancia — comisiones activas`;

  return {
    capitalInicial,
    balanceActual,
    gananciaNeta,
    enModoRecuperacion,
    rendimientoPct,
    comisionTotalCobrada,
    mensajeEstado,
  };
}

/**
 * Reconstruye comision acumulada y balance simulado a partir de una lista
 * cronologica de PnL por posicion. Util para el frontend cuando la DB
 * todavia no persiste comision_total_cobrada en tiempo real.
 */
export function reconstruirHistorico(
  capitalInicial: number,
  pnlsEnOrdenCronologico: number[],
): {
  balanceFinal: number;
  comisionTotalAdmin: number;
  gananciaTotalCliente: number;
  resultados: CommissionResult[];
} {
  let balance = capitalInicial;
  let comisionTotalAdmin = 0;
  let gananciaTotalCliente = 0;
  const resultados: CommissionResult[] = [];

  for (const pnl of pnlsEnOrdenCronologico) {
    const r = calcularComisionPosicion(capitalInicial, balance, pnl);
    resultados.push(r);
    balance += r.gananciaCliente; // El admin retira su parte de inmediato
    comisionTotalAdmin += r.comisionAdmin;
    gananciaTotalCliente += r.gananciaCliente;
  }

  return {
    balanceFinal: balance,
    comisionTotalAdmin,
    gananciaTotalCliente,
    resultados,
  };
}
