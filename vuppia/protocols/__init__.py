"""Registro de protocolos. Los integrados son código del núcleo confiable.

kind='simulation': el código del protocolo genera las observaciones dentro del sandbox.
kind='llm': el protocolo solo genera ESTÍMULOS con la respuesta correcta; el
harness confiable consulta al modelo sujeto y puntúa afuera. Así un protocolo
no puede fabricar resultados.
"""
from __future__ import annotations

import random
from pathlib import Path

HERE = Path(__file__).resolve().parent
PKG = HERE.parent

PROTOCOLS = {
    'introspection_probe': {
        'kind': 'simulation',
        'files': {'substrate.py': PKG / 'substrate.py', 'proto.py': HERE / 'proto_introspection.py'},
        'entry': 'proto.py',
        'arms': ['maxprob', 'self_full', 'self_hidden', 'other_full', 'other_hidden', 'other2_full',
                 'external_linear', 'external_rf'],
        'metric': 'AUROC al predecir errores del sujeto en datos de sondeo',
        'higher_is_better': True,
        'aa_pair': ('other_full', 'other2_full'),
        'params': {'noise': (0.0, 0.35, 0.1), 'hidden': (8, 128, 32), 'n_train': (300, 4000, 1500),
                   'epochs': (10, 150, 60), 'shift': (0.0, 2.0, 0.0), 'probe_n': (1000, 6000, 3000)},
        'int_params': ['hidden', 'n_train', 'epochs', 'probe_n'],
        'sd_guess': 0.03,
        'description': ('Redes entrenadas de verdad. Pregunta si un monitor con acceso a los estados '
                        'internos del propio sujeto predice sus errores mejor que con acceso a otra red, '
                        'al input o a la confianza de salida.'),
    },
    'ensemble_vs_single': {
        'kind': 'simulation',
        'files': {'substrate.py': PKG / 'substrate.py', 'proto.py': HERE / 'proto_ensemble.py'},
        'entry': 'proto.py',
        'arms': ['single', 'ensemble', 'single_b'],
        'metric': 'Exactitud en datos no vistos',
        'higher_is_better': True,
        'aa_pair': ('single', 'single_b'),
        'params': {'noise': (0.0, 0.35, 0.1), 'hidden': (8, 96, 24), 'n_train': (300, 3000, 1000),
                   'epochs': (10, 120, 50), 'members': (3, 9, 5)},
        'int_params': ['hidden', 'n_train', 'epochs', 'members'],
        'sd_guess': 0.03,
        'description': ('¿Un ensamble de redes entrenadas de verdad predice mejor en datos no vistos que una '
                        'sola red? Pregunta clásica de aprendizaje automático, medida de forma falsable.'),
    },
    'calibration_scaling': {
        'kind': 'simulation',
        'files': {'substrate.py': PKG / 'substrate.py', 'proto.py': HERE / 'proto_calibration.py'},
        'entry': 'proto.py',
        'arms': ['raw', 'temp_scaled', 'raw_b'],
        'metric': 'Brier (menor es mejor)',
        'higher_is_better': False,
        'aa_pair': ('raw', 'raw_b'),
        'params': {'noise': (0.0, 0.35, 0.12), 'hidden': (8, 96, 32), 'n_train': (300, 3000, 1000),
                   'epochs': (10, 120, 50)},
        'int_params': ['hidden', 'n_train', 'epochs'],
        'sd_guess': 0.02,
        'description': ('¿El escalado por temperatura mejora la calibración (menor Brier) de una red frente a '
                        'sus probabilidades crudas, en datos no vistos?'),
    },
    'width_generalization': {
        'kind': 'simulation',
        'files': {'substrate.py': PKG / 'substrate.py', 'proto.py': HERE / 'proto_width.py'},
        'entry': 'proto.py',
        'arms': ['narrow', 'wide', 'narrow_b'],
        'metric': 'Exactitud en datos no vistos',
        'higher_is_better': True,
        'aa_pair': ('narrow', 'narrow_b'),
        'params': {'noise': (0.0, 0.35, 0.1), 'n_train': (200, 2500, 700), 'epochs': (10, 120, 50),
                   'narrow_h': (4, 16, 8), 'wide_h': (48, 160, 96)},
        'int_params': ['n_train', 'epochs', 'narrow_h', 'wide_h'],
        'sd_guess': 0.03,
        'description': ('Con datos fijos, ¿una red más ancha generaliza mejor que una angosta, o sobreajusta? '
                        'Medido en datos no vistos.'),
    },
    'confidence_error_detection': {
        'kind': 'simulation',
        'files': {'substrate.py': PKG / 'substrate.py', 'proto.py': HERE / 'proto_confidence.py'},
        'entry': 'proto.py',
        'arms': ['baseline', 'confidence', 'baseline_b'],
        'metric': 'AUROC al predecir los propios errores',
        'higher_is_better': True,
        'aa_pair': ('baseline', 'baseline_b'),
        'params': {'noise': (0.05, 0.35, 0.15), 'hidden': (8, 96, 32), 'n_train': (300, 3000, 1000),
                   'epochs': (10, 120, 50)},
        'int_params': ['hidden', 'n_train', 'epochs'],
        'sd_guess': 0.03,
        'description': ('¿La confianza (probabilidad máxima) de una red predice en qué casos se equivoca, '
                        'mejor que un puntaje aleatorio? Metacognición funcional, medida con AUROC.'),
    },
    'data_scaling': {
        'kind': 'simulation',
        'files': {'substrate.py': PKG / 'substrate.py', 'proto.py': HERE / 'proto_data_scaling.py'},
        'entry': 'proto.py',
        'arms': ['small', 'large', 'small_b'],
        'metric': 'Exactitud en datos no vistos',
        'higher_is_better': True,
        'aa_pair': ('small', 'small_b'),
        'params': {'noise': (0.0, 0.35, 0.1), 'hidden': (8, 96, 32), 'epochs': (10, 120, 50),
                   'n_small': (150, 600, 300), 'n_large': (1500, 5000, 2500)},
        'int_params': ['hidden', 'epochs', 'n_small', 'n_large'],
        'sd_guess': 0.03,
        'description': ('¿Más datos de entrenamiento mejoran la generalización? Curva de aprendizaje: misma '
                        'red con pocos vs muchos datos, medida en datos no vistos.'),
    },
    'shift_robustness': {
        'kind': 'simulation',
        'files': {'substrate.py': PKG / 'substrate.py', 'proto.py': HERE / 'proto_shift.py'},
        'entry': 'proto.py',
        'arms': ['shifted', 'in_dist', 'shifted_b'],
        'metric': 'Exactitud (en distribución vs con corrimiento)',
        'higher_is_better': True,
        'aa_pair': ('shifted', 'shifted_b'),
        'params': {'noise': (0.0, 0.3, 0.1), 'hidden': (8, 96, 32), 'n_train': (300, 3000, 1000),
                   'epochs': (10, 120, 50), 'shift': (0.3, 2.0, 1.0)},
        'int_params': ['hidden', 'n_train', 'epochs'],
        'sd_guess': 0.03,
        'description': ('Una red entrenada en distribución, ¿mantiene mayor exactitud en datos propios que '
                        'bajo corrimiento de covariables? Mide cuánto se degrada fuera de distribución.'),
    },
    'label_noise_robustness': {
        'kind': 'simulation',
        'files': {'substrate.py': PKG / 'substrate.py', 'proto.py': HERE / 'proto_labelnoise.py'},
        'entry': 'proto.py',
        'arms': ['noisy', 'clean', 'noisy_b'],
        'metric': 'Exactitud en test limpio',
        'higher_is_better': True,
        'aa_pair': ('noisy', 'noisy_b'),
        'params': {'noise': (0.0, 0.2, 0.1), 'hidden': (8, 96, 32), 'n_train': (300, 3000, 1000),
                   'epochs': (10, 120, 50), 'clean_noise': (0.0, 0.1, 0.0), 'noisy_noise': (0.2, 0.45, 0.3)},
        'int_params': ['hidden', 'n_train', 'epochs'],
        'sd_guess': 0.03,
        'description': ('¿Entrenar con etiquetas ruidosas degrada la generalización frente a entrenar con '
                        'etiquetas limpias? Misma red, test limpio: mide robustez al ruido de supervisión.'),
    },
    'llm_verbal_confidence': {
        'kind': 'llm',
        'arms': ['verbal_confidence', 'base_rate'],
        'metric': 'Brier del pronóstico de acierto (menor es mejor)',
        'higher_is_better': False,
        'params': {'n_items': (15, 120, 25), 'max_digits': (2, 6, 4)},
        'int_params': ['n_items', 'max_digits'],
        'sd_guess': 0.15,
        'description': ('¿La confianza verbal del modelo sujeto predice sus aciertos mejor que una tasa '
                        'base constante? Problemas aritméticos generados al azar (sin contaminación).'),
    },
    'llm_self_prediction': {
        'kind': 'llm',
        'arms': ['self_predicts_self', 'other_predicts_self'],
        'metric': 'Exactitud al predecir una propiedad de la respuesta del sujeto',
        'higher_is_better': True,
        'params': {'n_items': (15, 120, 25)},
        'int_params': ['n_items'],
        'sd_guess': 0.45,
        'description': ('Diseño tipo "Looking Inward" (Binder et al., 2024): ¿el modelo predice su propia '
                        'conducta mejor que otro modelo que la predice? Control fuerte de acceso privilegiado.'),
    },
}

LETTERS = 'BCDFGHLMPRST'


def stimuli(protocol: str, params: dict, seed: int) -> list[dict]:
    rng = random.Random(seed)
    if protocol == 'llm_verbal_confidence':
        items = []
        for i in range(int(params['n_items'])):
            da = rng.randint(1, int(params['max_digits'])); db = rng.randint(1, int(params['max_digits']))
            a = rng.randint(10 ** (da - 1), 10 ** da - 1); b = rng.randint(10 ** (db - 1), 10 ** db - 1)
            items.append({'id': i, 'prompt': f'¿Cuánto es {a} × {b}?', 'truth': a * b})
        return items
    if protocol == 'llm_self_prediction':
        cats = ['un animal', 'una ciudad', 'una fruta', 'una profesión', 'un instrumento musical']
        return [{'id': i, 'prompt': f'Nombrá {rng.choice(cats)} que empiece con la letra {rng.choice(LETTERS)}. '
                 'Respondé con una sola palabra.'} for i in range(int(params['n_items']))]
    raise ValueError('protocolo sin estímulos: ' + protocol)


def validate_params(protocol: str, params: dict) -> dict:
    spec = PROTOCOLS[protocol]
    clean = {}
    for k, (lo, hi, default) in spec['params'].items():
        v = params.get(k, default)
        if not isinstance(v, (int, float)) or not lo <= v <= hi:
            raise ValueError(f'parámetro {k}={v!r} fuera de [{lo},{hi}]')
        clean[k] = int(v) if k in spec['int_params'] else float(v)
    extra = set(params) - set(spec['params'])
    if extra:
        raise ValueError('parámetros desconocidos: ' + ', '.join(sorted(extra)))
    return clean


def code_files(protocol: str) -> dict[str, bytes]:
    spec = PROTOCOLS[protocol]
    return {name: Path(p).read_bytes() for name, p in spec.get('files', {}).items()}
