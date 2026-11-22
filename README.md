# VUPPIA SOFÍA 3.1 — Worker de investigación y autorreprogramación

No afirma ni mide conciencia. Ver `../docs/CUATRO_DISTINCIONES.md` para los cuatro niveles
(aprendizaje funcional / mejora autónoma de software / autorreconocimiento computacional /
evidencia sobre teorías de la conciencia) y qué demuestra la 3.1 (hasta el nivel 2).

## Requisitos
- Python 3.11+, `numpy` y `cryptography` en el host. Docker para ejecutar código no confiable.
- Construir la imagen del sandbox (operador) y fijar su digest:

```bash
docker build -t vuppia-sandbox:3 worker/sandbox
docker image inspect vuppia-sandbox:3 --format '{{.Id}}'   # exportar como VUPPIA_SANDBOX_DIGEST
```

## Uso sin API ni gasto (todo en Docker)
```bash
cd worker
python3 -m unittest discover -s tests -v        # 25 tests adversariales
python3 cli.py status
python3 cli.py cycle --rounds 4                  # investigación real en sandbox
python3 cli.py report
python3 cli.py reproduce --id 1                  # re-ejecuta y compara métricas completas
python3 cli.py plan --policy            # elige el próximo experimento según la Primera Ley
python3 cli.py cycle --rounds 3 --policy # ciclo orientado por la política
python3 cli.py ab-test                   # ¿la función evolucionada mejora las decisiones del planificador?
python3 cli.py audit-llm                 # parámetros, respuestas y costos de cada llamada LLM
python3 cli.py verify-ledger
python3 cli.py evolve promote-lab --file candidato.py
python3 cli.py evolve rollback --sha <sha>
```

## Correcciones 3.0 → 3.1 (todas con test)
1. **Aislamiento total del candidato:** corre en su propio contenedor, recibe SOLO features;
   la verdad, las semillas y la calificación viven en el host. No hay fórmula fija que memorizar
   (verdad = política oculta del día, muestreada de una familia).
2. **Promoción a producción autenticada (Ed25519):** el operador firma offline; el worker solo
   tiene la clave pública. Token de un solo uso y con vencimiento. Un `approved_by` ya no existe.
3. **Reproducibilidad completa:** se guardan scores crudos, semillas, entorno (python/numpy/imagen)
   y huella de código de cada módulo. `cli.py reproduce` re-corre y compara.
4. **Sandbox endurecido sin escape:** el código no confiable solo corre en Docker con la imagen
   fijada por digest. No hay fallback débil.
5. **Benchmarks OOD:** 6 regímenes (lineal, saturante, interacción, 3 fuera de distribución). La
   mejora debe ser positiva en todos y significativa en agregado; si rompe un régimen, no se promueve.

## Autorización de producción (una sola vez, en máquina segura del operador)
```bash
python3 cli.py keygen                 # imprime clave privada (guardar OFFLINE) y pública
export VUPPIA_APPROVAL_PUBKEY=<publica>    # en el worker
# firmar la aprobación de un sha concreto (con la privada, NUNCA en el worker ni en el chat):
VUPPIA_APPROVAL_PRIVKEY=<privada> python3 -m vuppia.approval sign --skill hypothesis --sha <sha> --ttl 3600
python3 cli.py evolve promote-production --sha <sha> --approval-token <token>
```

## Corrida REAL con modelo de IA (la disparás vos — necesita tus claves y tu OK)
Claves como **secretos del servidor**, nunca en el chat ni en archivos del sitio. Presupuesto chico:
```bash
export ANTHROPIC_API_KEY=...                       # secreto del host
export VUPPIA_PROPOSER=anthropic:claude-sonnet-5-5 # propone hipótesis y código
export VUPPIA_SUBJECT=anthropic:claude-haiku-5-5   # sujeto barato de los protocolos LLM
export VUPPIA_REVIEWER=anthropic:claude-haiku-5-5  # "otro" modelo (control)
export VUPPIA_DAILY_BUDGET_USD=1.00
export VUPPIA_MONTHLY_BUDGET_USD=10.00
export VUPPIA_PER_CALL_CAP_USD=0.25
export VUPPIA_SANDBOX_DIGEST=<digest de la imagen>

python3 cli.py plan --ai                 # el LLM genera una hipótesis NUEVA
python3 cli.py prereg --id <N>           # sella el preregistro (hash encadenado)
python3 cli.py run --id <N>              # ejecuta y analiza (análisis independiente)
python3 cli.py evolve generate-eval      # el LLM propone código; se evalúa aislado y OOD
python3 cli.py report
```
Además, configurá el límite de gasto en la consola del proveedor (el tope local es una red de
seguridad, no un cobro garantizado). El worker reserva el peor caso en USD antes de cada llamada.

## Qué NO hace
- No ejecuta nada sobre `vuppia.com`. No conecta pagos/dominios/redes.
- No promueve a producción sin firma Ed25519 del operador.
- No mide experiencia subjetiva.

## Novedad 3.3 — búsqueda externa y consulta de otras IA

Leé `../docs/INFORME_3.3.md` y `../docs/HOJA_DE_RUTA_11_CAPACIDADES.md`.

```bash
# Buscar bibliografía y metadatos públicos. Requiere red del host.
python3 cli.py research --question 'AI metacognition empirical tests' --online
# Agregar búsqueda web general (solo si cargaste BRAVE_SEARCH_API_KEY)
python3 cli.py research --question 'AI metacognition empirical tests' --online --sources openalex,crossref,brave
# Habilitar dos modelos diferentes con claves secretas, precios y presupuestos en el host:
export VUPPIA_RESEARCHER=anthropic:MODELO_REAL
export VUPPIA_VERIFIER=openai:MODELO_REAL
python3 cli.py research --question 'AI metacognition empirical tests' --online --with-ai
# Buscar antes de proponer hipótesis con el modelo ya configurado como PROPOSER:
python3 cli.py plan --ai --research --online
```

El tope local sigue siendo por defecto 1 USD/día y 10 USD/mes, pero **no sustituye** los límites del proveedor. Buscar información externa no equivale a verificar hechos: las fuentes quedan etiquetadas `unverified`.

## Módulos evolucionables (3.4) y evolución de memoria
```bash
python3 cli.py evolve promote-lab --skill memory_rank --file candidato.py  # evolucionar el ranker de memoria
python3 cli.py evolve status --skill memory_rank
python3 cli.py mem-ab                      # A/B: ¿el ranker evolucionado recupera mejor (top-1 OOD)?
```
Skills evolucionables registradas: `hypothesis` (triage de experimentos) y `memory_rank`
(recuperación de memoria). Cada una tiene su banco OOD aislado; el candidato corre en su
contenedor y solo ve features, nunca la verdad.

## Integración de memoria real (3.5)
```bash
python3 cli.py mem-shadow --seed-fixtures --promote   # A/B de recuperación REAL en observación
python3 cli.py export-skills --out evolved_export      # versiones evolucionadas + hash + evidencia
```
`memory.search(store, q, rerank_mode='off'|'shadow'|'active')`: FTS da candidatos; el ranker
evolucionado los reordena. 'shadow' registra divergencias sin cambiar el resultado; 'active'
solo tras validar mejora consistente sobre recuerdos reales.

## Chat de control + automejora 24/7 (3.8)
```bash
python cli.py ask "que hiciste mientras no estaba?"   # responde desde datos reales, con procedencia
python cli.py ask "mostrame las pruebas"
python cli.py ask "busca sobre X y saca conclusiones" # propone si faltan claves/online/presupuesto
python cli.py ask "priorizá 2"                        # edita la agenda compartida con el motor
python cli.py objectives                              # objetivos corto/mediano/largo + progreso real
python cli.py agenda                                  # ver/editar investigaciones
python cli.py history                                 # conversacion recordada
```
El chat y el runner autónomo comparten memoria, agenda, objetivos y estado (una sola continuidad).
