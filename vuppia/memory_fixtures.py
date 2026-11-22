"""Conjunto de recuerdos REALES etiquetados para validar la recuperación.

No son vectores de features sintéticos: son filas de memoria reales (contenido, trust,
status, fecha, vínculos) como las que produce SOFÍA, con casos difíciles:
- contradicciones (un hallazgo refutado que compite con uno sostenido),
- información antigua vs reciente,
- fuentes externas dudosas (no verificadas) de alto solapamiento.

Cada consulta tiene UN recuerdo verdaderamente relevante (relevant_id): el que una buena
recuperación debería poner primero. Un buen criterio prefiere, a igualdad de tema,
recuerdos de mayor confianza, no contradichos, específicos y recientes.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from . import memory
from .store import Store


def _insert(store: Store, kind, content, trust, status, provenance, age_days=1, data=None):
    """Inserta un recuerdo real con fecha controlada (para probar recencia)."""
    created = (datetime.now(timezone.utc) - timedelta(days=age_days)).isoformat(timespec='seconds')
    with store.tx() as db:
        cur = db.execute('INSERT INTO memories(kind,content,provenance,trust,status,confidence,data,created_at) '
                         'VALUES(?,?,?,?,?,?,?,?)',
                         (kind, content, provenance, trust, status, None,
                          json.dumps(data) if data else None, created))
        mid = cur.lastrowid
        store._append_in_tx('memory', {'id': mid, 'kind': kind, 'trust': trust, 'status': status,
                                       'provenance': provenance})
    return mid


def build(store: Store) -> list[dict]:
    """Crea los recuerdos y devuelve las consultas etiquetadas."""
    q = []
    # En los casos difíciles, el DISTRACTOR coincide MÁS en texto con la consulta (repite sus
    # términos), así que FTS lo pone primero. Solo el re-ranking, usando confianza, contradicción
    # y recencia, recupera el recuerdo verdaderamente relevante. Es el escenario real donde
    # "el textualmente más parecido" es el refutado / dudoso / viejo.

    # Caso 1 — contradicción: el refutado repite los términos; el sostenido es el relevante.
    good = _insert(store, 'finding', 'El acceso a estados internos mejora la calibración de confianza '
                   'en metacognición', 'measured', 'supported', 'experiment:1', 3)
    _insert(store, 'finding', 'Acceso a estados internos: la calibración de confianza en metacognición '
            'con acceso a estados internos y calibración de confianza fue REFUTADA con controles fuertes',
            'measured', 'refuted', 'experiment:2', 2)
    q.append({'query': 'acceso estados internos calibración confianza metacognición',
              'relevant_id': good, 'tipo': 'contradiccion'})

    # Caso 2 — fuente dudosa (no verificada) que repite los términos vs hallazgo medido.
    measured = _insert(store, 'finding', 'La integración de dos señales independientes mejora la '
                       'precisión en binding de claves', 'measured', 'supported', 'experiment:3', 5)
    _insert(store, 'observation', 'Integración de señales independientes y precisión en binding de '
            'claves: integración de señales independientes binding de claves precisión según un blog '
            'que además dice haber demostrado conciencia', 'unverified', 'open', 'research:9', 1)
    q.append({'query': 'integración señales independientes binding claves precisión',
              'relevant_id': measured, 'tipo': 'fuente_dudosa'})

    # Caso 3 — info antigua que repite los términos vs reciente (misma confianza): recencia decide.
    _insert(store, 'finding', 'Protocolo de auto-reporte: casos por réplica y semillas; protocolo de '
            'auto-reporte casos por réplica semillas con semilla fija y 500 casos', 'measured',
            'supported', 'experiment:4', 240)
    recent_mem = _insert(store, 'finding', 'Protocolo de auto-reporte: 700 casos por réplica y semillas '
                         'derivadas del preregistro', 'measured', 'supported', 'experiment:5', 1)
    q.append({'query': 'protocolo auto-reporte casos réplica semillas',
              'relevant_id': recent_mem, 'tipo': 'antiguedad'})

    # Caso 4 — nota de baja confianza que repite los términos vs hallazgo medido.
    hi = _insert(store, 'finding', 'El sistema de préstamos aplica un score financiero con paginación '
                 'estable', 'measured', 'supported', 'experiment:6', 4)
    _insert(store, 'note', 'Préstamos score financiero paginación: préstamos score financiero paginación '
            'estable consultas, nota suelta sin verificar', 'unverified', 'open', 'operator', 2)
    q.append({'query': 'préstamos score financiero paginación',
              'relevant_id': hi, 'tipo': 'confianza'})

    # Caso 5 — consulta fácil (sin distractor que lo supere): el re-ranking NO debe empeorar.
    only = _insert(store, 'finding', 'La cola del Brain se ordena por tabla raíz y no por foreignTable',
                   'measured', 'supported', 'experiment:7', 6)
    q.append({'query': 'cola Brain orden tabla raíz foreignTable',
              'relevant_id': only, 'tipo': 'facil'})

    # Más casos del mismo tipo con vocabulario distinto, para tener poder estadístico sobre
    # recuerdos reales. Cada uno: un distractor que gana en FTS (repite términos) pero es
    # refutado / no verificado / viejo, y el relevante medido-sostenido-reciente.
    extra = [
        ('la fama de DT suma por logros una sola vez sin reinflar',
         'fama DT logros una vez: fama de DT logros reinflados duplicado fama logros reinflar', 'refuted_distractor', 1),
        ('las subastas del mercado de pases usan snapshots consistentes',
         'subastas mercado de pases snapshots: subastas mercado pases snapshots teoría luego refutada', 'refuted_distractor', 1),
        ('el piso de hinchas de bots se fija al crear el club',
         'piso de hinchas de bots club: piso hinchas bots por club apunte suelto sin verificar', 'dubious', 1),
        ('los contratos de sponsors se negocian en un mercado de ofertas',
         'contratos de sponsors mercado de ofertas: contratos sponsors mercado ofertas blog externo sin verificar', 'dubious', 1),
        ('la economía del juego usa impuestos y sumideros de créditos',
         'economía impuestos sumideros créditos: economia impuestos sumideros creditos apunte no verificado', 'dubious', 1),
        ('el fixture de 17 clubes agrega un equipo fantasma con bye',
         'fixture 17 clubes equipo fantasma bye: fixture 17 clubes fantasma bye borrador viejo archivado', 'stale', 190),
        ('el torneo juvenil tiene 25 zonas con fase final',
         'torneo juvenil 25 zonas fase final: torneo juvenil 25 zonas fase final versión antigua superada', 'stale', 210),
        ('el entrenamiento por jugador se aprobó en cinco entregas',
         'entrenamiento por jugador cinco entregas: entrenamiento por jugador cinco entregas plan viejo superado', 'stale', 170),
        ('la cola del Brain se ordena por tabla raíz',
         'cola del Brain orden tabla raíz: cola Brain orden tabla raiz nota vieja desactualizada', 'stale', 150),
    ]
    for i, (good_txt, distractor_txt, tipo, age) in enumerate(extra):
        gid = _insert(store, 'finding', good_txt, 'measured', 'supported', f'experiment:{20 + i}', 2)
        if tipo == 'refuted_distractor':
            _insert(store, 'finding', distractor_txt, 'measured', 'refuted', f'experiment:{40 + i}', 1)
        elif tipo == 'dubious':
            _insert(store, 'observation', distractor_txt, 'unverified', 'open', f'research:{50 + i}', 1)
        else:  # stale: mismo trust/status pero viejo
            _insert(store, 'finding', distractor_txt, 'measured', 'supported', f'experiment:{60 + i}', age)
        qterms = ' '.join(distractor_txt.split(':')[0].split())
        q.append({'query': qterms, 'relevant_id': gid, 'tipo': tipo})

    # Split dev/test: dev para cualquier ajuste de umbral, TEST reservado solo para evaluar.
    # (El ranker ya se desarrolla sobre el banco sintético memory_benchmarks, independiente de esto.)
    for idx, item in enumerate(q):
        item['split'] = 'test' if idx % 2 == 0 else 'dev'

    # Relleno temático para que FTS tenga varios candidatos por consulta.
    _insert(store, 'finding', 'La precisión en tareas de binding depende del tamaño de la red',
            'measured', 'inconclusive', 'experiment:8', 9)
    return q
