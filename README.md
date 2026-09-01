# Motor de decisión sobre flujos de mercado en tiempo real

Sistema propio de **ingesta continua, evaluación y decisión** sobre datos de mercado, con
registro auditable de cada acción. Construido y operado por
**[Diego Mauricio García R.](https://www.datovatenexuspro.com)** — Pereira, Colombia.

> **Opera en cuenta demo y con `DRY_RUN` activo.** No es un producto financiero ni una
> recomendación de inversión. Es la arquitectura la que se muestra aquí, no una estrategia.

---

## Por qué está público

La mayoría de los sistemas que construyo son de clientes y no se pueden mostrar. Este es
mío, así que se puede leer completo. Y el problema que resuelve es el mismo que resuelvo
para las empresas que me contratan, solo que con otro tipo de dato:

**un flujo que no para, decisiones que hay que tomar en milisegundos, y la obligación de
poder explicar después por qué se tomó cada una.**

Cambia "mercado" por "inventario", "cartera" o "pedidos" y es exactamente el mismo
problema de ingeniería.

## Qué hay dentro

| Capa | Qué hace |
|---|---|
| **Ingesta** | Consumo continuo de flujos, normalización y persistencia en PostgreSQL |
| **Evaluación** | Reglas y modelos que puntúan cada situación con su nivel de confianza |
| **Decisión** | Ejecuta o veta, con el motivo registrado — nunca una decisión sin rastro |
| **Panel** | Interfaz en Next.js sobre el estado real del sistema, no sobre una simulación |
| **Operación** | Despliegue propio en contenedores, monitoreo y registro permanente |

`116` archivos Python · `50` TypeScript · `31` SQL · `22` React

## Las reglas que no negocio

Son las mismas de todas mis arquitecturas, y aquí se pueden verificar en el código:

1. **Aislamiento de lo crítico.** Ningún experimento puede tocar lo que está operando.
2. **`DRY_RUN` por defecto.** Nada pasa a ejecución real sin haber pasado el protocolo de
   validación completo.
3. **Ninguna afirmación sin evidencia.** Todo comportamiento que se afirme del sistema va
   acompañado de su registro con marca de tiempo. Lo no verificado se declara hipótesis.

Esa tercera regla es la que aplico también cuando diagnostico la operación de un cliente:
nada se afirma sin verificarse contra datos reales.

## Stack

`Python` · `TypeScript` · `Next.js` · `PostgreSQL` · `Docker` · despliegue propio

---

**¿Tu operación tiene un flujo que no para y decisiones que hoy se toman a mano?**
Hablemos: [datovatenexuspro.com](https://www.datovatenexuspro.com) ·
diegomao.201@gmail.com · WhatsApp +57 320 504 6277
