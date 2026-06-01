import json
import os
from collections import Counter

def audit(file_path):
    if not os.path.exists(file_path):
        print(f"Archivo no encontrado: {file_path}")
        return
        
    with open(file_path, 'r') as f:
        data = json.load(f)

    # Tomamos las últimas 100 operaciones
    trades = data[-100:]
    if not trades:
        print("No hay suficientes operaciones.")
        return

    wins = []
    losses = []
    reasons = Counter()

    for t in trades:
        # Extrae el PnL unificando formato de Binance y Deriv
        pnl = float(t.get("pnl_usdt", t.get("realized_pnl_usdt", 0)))
        if pnl > 0:
            wins.append(pnl)
        else:
            losses.append(pnl)
        reasons[t.get("exit_reason", "unknown")] += 1

    win_rate = (len(wins) / len(trades)) * 100
    print(f"--- AUDITORIA DE LAS ULTIMAS {len(trades)} OPERACIONES ---")
    print(f"Win Rate: {win_rate:.2f}%")
    print(f"Ganadoras: {len(wins)} | Perdedoras: {len(losses)}")
    print(f"PnL Total de esta muestra: {sum(wins) + sum(losses):.4f} USDT")
    
    print("\nRazones de salida más comunes:")
    for r, count in reasons.most_common(5):
        print(f" - {r}: {count}")

print(">> Evaluando Binance...")
audit("logs/closed_trades.json")
print("\n>> Evaluando Deriv...")
audit("logs/deriv_closed_contracts.json")