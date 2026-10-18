# Clasificación de Alertas por Criticidad con Jev

## Resumen ejecutivo

Este documento describe la integración de Jev (TypeSafe AI's typed classification model) para clasificar automáticamente las alertas de sobreconsumo de combustible por nivel de criticidad. Jev proporciona clasificaciones rápidas (70–500ms), tipadas, y con scores de confianza, lo que permite enrutamiento determinístico y consistente de alertas críticas al gestor de flota.

## ¿Qué es Jev?

Jev es un **modelo de decisión** (no un LLM generativo) optimizado para clasificación de texto a escala. A diferencia de los LLMs que generan texto libre, Jev devuelve respuestas estructuradas y tipadas que nunca salen del conjunto de opciones definidas.

### Características clave

- **Latencia**: 70–500ms por clasificación
- **Costo**: $0.042 por millón de tokens de entrada; tokens de salida gratis (~$0.026 por 1,000 items)
- **Salida tipada**: Nunca inventa una clase fuera del conjunto definido
- **Confidence scoring**: Cada clasificación incluye un score de confianza (0–1)
- **Calibrado**: Las probabilidades reportadas están calibradas con RLCD
- **Velocidad**: ~40–200x más rápido que un LLM para clasificación

### Tipos de preguntas

1. **Choice**: Selecciona exactamente una opción de hasta 255 opciones (para categorías exclusivas)
2. **Noul**: Pregunta sí/no que devuelve una probabilidad entre 0 y 1
3. **Score**: Pregunta numérica (no usada en este diseño)

### Límites de determinismo

**Jev NO es estrictamente determinístico**. Según la documentación oficial y pruebas empíricas:

- Con modelo fijo (`jev-1.13.0`), requests idénticas **usualmente** devuelven la misma clasificación
- Las probabilidades pueden variar por algunos centésimos (±0.01–0.03)
- En casos borderline, la clasificación puede cambiar (~2 de 20 en un test de 30 repeticiones)
- La redacción de preguntas y definiciones de opciones tiene **mucho más impacto** que la variación por repetición
- TypeSafe describe Jev como diseñado para **consistencia**, no para determinismo estricto

## Flujo actual de alertas en Fleet Intelligence

### Modelo de datos actual

Según los issues #14, #15 y el product context, el sistema tiene:

1. **Telemetría**: Lecturas GPS y de combustible de vehículos (`x7001` L/h, `x7002` L acumulados)
2. **Línea base**: Consumo esperado por tramo y sentido de corredor, calculado con pares (días 1–12)
3. **Sobreconsumo**: Cuando el consumo observado supera la línea base del tramo más allá de un umbral
4. **Anomalía**: Hallazgo registrado que agrupa tramos consecutivos con sobreconsumo de un mismo viaje
5. **Reporte del conductor**: Mensajes de WhatsApp asociados al viaje (ej. "esperando descarga", "parada para ir al baño")
6. **Alerta**: Notificación lista para enviar al gestor de flota por WhatsApp, con evidencia

### Flujo de detección

```
Telemetría → Cálculo de consumo por tramo → Comparación con línea base
                                                    ↓
                                              Sobreconsumo detectado
                                                    ↓
                                         Agrupación en anomalía
                                                    ↓
                              ┌─────────────────────┴─────────────────────┐
                              ↓                                           ↓
                    Correlación con reportes                    Ventana anti-spam
                         del conductor                                   ↓
                              └─────────────────────┬─────────────────────┘
                                                    ↓
                                            Alerta registrada
                                                    ↓
                                          Envío por WhatsApp
```

### Problema actual

**No existe clasificación por criticidad**. Todas las alertas se tratan igual, sin priorización automática. El gestor de flota recibe todas las alertas en el mismo canal sin indicación de urgencia o severidad.

## Integración de Jev: Dónde encaja

### Punto de inserción

Jev se integra **después de que se registra la anomalía y antes de generar la alerta**, añadiendo un campo `criticality` (o `severity`) al registro de alerta.

```
Anomalía registrada
       ↓
       ├─→ Evidencia (tramos, consumo, duración, ralentí)
       ├─→ Reporte del conductor (si existe)
       ↓
  [NUEVO] Clasificación Jev
       ↓
Alerta con criticidad
       ↓
Política anti-spam por nivel
       ↓
Envío WhatsApp con emoji de severidad
```

### Niveles de criticidad propuestos

Basado en el dominio de transporte de carga y combustible:

| Nivel | Nombre | Descripción | Acción esperada |
|-------|--------|-------------|-----------------|
| `critical` | Crítico | Sobreconsumo severo sin explicación del conductor; ralentí prolongado (>1h); anomalía en progreso con impacto alto | Revisar inmediatamente; llamar al conductor |
| `high` | Alto | Sobreconsumo significativo (>30% sobre línea base); ralentí moderado (30–60 min) sin justificación | Revisar en <1 hora; preparar acción |
| `medium` | Medio | Sobreconsumo moderado (15–30%); explicado por conductor (ej. "esperando descarga"); múltiples tramos afectados | Monitorear; revisar cuando termine el viaje |
| `low` | Bajo | Sobreconsumo leve (<15%); un solo tramo; causa conocida (ej. pendiente pronunciada) | Registro; sin acción inmediata |

**Nota**: Los umbrales exactos (%, minutos) se calibran durante implementación contra datos reales del replay FEAD.

## Diseño de la clasificación Jev

### Pregunta Jev

Una sola pregunta **Choice** que selecciona exactamente un nivel de criticidad:

```typescript
{
  "model": "typesafe/jev-1.13.0",  // Versión fija para reproducibilidad
  "state": {
    "alert_context": "Vehículo T28 en flete F-104: sobreconsumo de 42.3 L/100km vs 32.5 L/100km de línea base (30% sobre baseline). Afectó 3 tramos consecutivos (30 km). Ralentí detectado: 45 minutos. Reporte del conductor: ninguno. Viaje en progreso, restante: 280 km."
  },
  "questions": {
    "criticality": {
      "type": "choice",
      "instructions": "Clasifica la criticidad de esta alerta de sobreconsumo de combustible para el gestor de flota.",
      "criteria": {
        "critical": "Sobreconsumo severo (>40% sobre baseline) SIN explicación del conductor, o ralentí prolongado >60min sin justificar, o anomalía activa con impacto económico muy alto. Requiere acción inmediata.",
        "high": "Sobreconsumo significativo (25–40%) sin explicación clara, o ralentí 30–60min sin justificar, o múltiples tramos afectados (>20km) sin causa conocida. Requiere revisión pronto.",
        "medium": "Sobreconsumo moderado (15–25%), o explicado por conductor (ej. 'esperando descarga', 'tráfico'), o ralentí <30min, o patrón no urgente. Monitoreo requerido.",
        "low": "Sobreconsumo leve (<15%), o un solo tramo aislado, o causa evidente (pendiente pronunciada, condiciones de ruta), o ya resuelto. Solo registro."
      }
    }
  }
}
```

### Construcción del contexto (state)

El `state` se construye a partir de:

1. **Identificadores**: Vehículo (alias anónimo como T28), flete
2. **Métricas de consumo**: Observado vs línea base, porcentaje sobre baseline
3. **Alcance**: Número de tramos afectados, distancia total
4. **Ralentí**: Duración total detectada
5. **Reporte del conductor**: Texto original si existe, o "ninguno"
6. **Estado del viaje**: En progreso / completado, distancia restante
7. **Evidencia adicional**: Paradas prolongadas, desvíos, etc.

**Longitud típica**: 150–300 tokens (~600–1,200 caracteres)

### Respuesta esperada

```json
{
  "model": "typesafe/jev-1.13.0",
  "answers": {
    "criticality": {
      "type": "choice",
      "choice": "high",
      "confidence": 0.89,
      "probabilities": {
        "critical": 0.08,
        "high": 0.74,
        "medium": 0.16,
        "low": 0.02
      }
    }
  },
  "usage": {
    "input_tokens": 248,
    "output_tokens": 0,
    "cost": 0.000010416
  }
}
```

## Estrategia de reproducibilidad

Dado que Jev no es estrictamente determinístico, implementamos múltiples capas de consistencia:

### 1. Versión fija del modelo

```python
JEV_MODEL = "typesafe/jev-1.13.0"  # NUNCA usar jev-latest
```

### 2. Cache por hash de request

```python
def build_cache_key(anomaly_evidence: dict) -> str:
    """
    Construye un hash determinístico del contexto de la anomalía.
    El mismo conjunto de evidencia siempre produce el mismo key.
    """
    # Normalizar valores numéricos a 2 decimales para evitar drift de float
    normalized = {
        "vehicle_id": anomaly_evidence["vehicle_id"],
        "trip_id": anomaly_evidence["trip_id"],
        "overconsumption_pct": round(anomaly_evidence["overconsumption_pct"], 2),
        "idle_minutes": round(anomaly_evidence["idle_minutes"], 1),
        "segments_affected": anomaly_evidence["segments_affected"],
        "driver_report": anomaly_evidence.get("driver_report", ""),
        # Incluir versión del modelo en el hash
        "model": JEV_MODEL,
    }
    return hashlib.sha256(json.dumps(normalized, sort_keys=True).encode()).hexdigest()
```

**Tabla de cache** (PostgreSQL):

```sql
CREATE TABLE jev_classification_cache (
    cache_key TEXT PRIMARY KEY,
    anomaly_id UUID NOT NULL,
    criticality TEXT NOT NULL,  -- critical, high, medium, low
    confidence NUMERIC(5,4) NOT NULL,
    probabilities JSONB NOT NULL,
    model_version TEXT NOT NULL,
    classified_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    request_context JSONB NOT NULL,  -- Para auditoría
    CONSTRAINT criticality_valid CHECK (criticality IN ('critical', 'high', 'medium', 'low'))
);

CREATE INDEX idx_jev_cache_anomaly ON jev_classification_cache(anomaly_id);
CREATE INDEX idx_jev_cache_classified_at ON jev_classification_cache(classified_at);
```

**Política de retención**: Cache permanente (para auditoría y reproducibilidad); 90 días de hot cache, luego archivo.

### 3. Reglas determinísticas de override

Para casos extremos donde la evidencia es inequívoca, aplicar reglas antes de Jev:

```python
def deterministic_override(evidence: dict) -> str | None:
    """
    Reglas determinísticas que evitan llamada a Jev para casos obvios.
    Retorna criticality o None si requiere clasificación Jev.
    """
    # Crítico: sobreconsumo masivo sin explicación
    if (evidence["overconsumption_pct"] > 50 
        and not evidence.get("driver_report") 
        and evidence["trip_status"] == "in_progress"):
        return "critical"
    
    # Crítico: ralentí extremo sin justificación
    if evidence["idle_minutes"] > 90 and not evidence.get("driver_report"):
        return "critical"
    
    # Bajo: sobreconsumo mínimo en un solo tramo
    if evidence["overconsumption_pct"] < 10 and evidence["segments_affected"] == 1:
        return "low"
    
    # Bajo: viaje completado, sobreconsumo explicado
    if (evidence["trip_status"] == "completed" 
        and evidence.get("driver_report") 
        and evidence["overconsumption_pct"] < 20):
        return "low"
    
    return None  # Requiere clasificación Jev
```

### 4. Threshold de confianza para review humana

```python
CONFIDENCE_THRESHOLD = 0.70

def requires_human_review(confidence: float, probabilities: dict[str, float]) -> bool:
    """
    Determina si la clasificación debe ir a revisión humana.
    
    - Confianza baja (<0.70)
    - Probabilidades muy cercanas entre dos niveles contiguos (diff <0.15)
    """
    if confidence < CONFIDENCE_THRESHOLD:
        return True
    
    # Ordenar probabilidades
    sorted_probs = sorted(probabilities.items(), key=lambda x: x[1], reverse=True)
    top_two_diff = sorted_probs[0][1] - sorted_probs[1][1]
    
    return top_two_diff < 0.15
```

### 5. Versionamiento de preguntas y criterios

```python
# backend/app/jev/questions.py
JEV_QUESTION_VERSION = "1.0.0"

JEV_CRITICALITY_QUESTION = {
    "version": JEV_QUESTION_VERSION,
    "model": "typesafe/jev-1.13.0",
    "type": "choice",
    "instructions": "Clasifica la criticidad de esta alerta de sobreconsumo de combustible para el gestor de flota.",
    "criteria": {
        "critical": "Sobreconsumo severo (>40% sobre baseline)...",
        # ... (texto completo arriba)
    }
}
```

Cuando cambie la redacción, incrementar `JEV_QUESTION_VERSION` e invalidar cache antiguo.

## Arquitectura de implementación

### Componentes

```
┌─────────────────────────────────────────────────────────────┐
│                       Domain API (FastAPI)                   │
├─────────────────────────────────────────────────────────────┤
│                                                               │
│  ┌────────────────────────────────────────────────────────┐ │
│  │ AnomalyDetector                                         │ │
│  │  └─> crea Anomaly con evidencia                        │ │
│  └────────────┬───────────────────────────────────────────┘ │
│               ↓                                               │
│  ┌────────────────────────────────────────────────────────┐ │
│  │ AlertClassifier (NEW)                                   │ │
│  │  ├─> deterministic_override()                           │ │
│  │  ├─> build_cache_key() → cache lookup                  │ │
│  │  ├─> JevClient.classify() [si no hay cache]            │ │
│  │  ├─> requires_human_review()                            │ │
│  │  └─> guarda en cache + retorna criticality             │ │
│  └────────────┬───────────────────────────────────────────┘ │
│               ↓                                               │
│  ┌────────────────────────────────────────────────────────┐ │
│  │ AlertGenerator                                          │ │
│  │  └─> crea Alert con criticality + anti-spam policy     │ │
│  └────────────┬───────────────────────────────────────────┘ │
│               ↓                                               │
│  ┌────────────────────────────────────────────────────────┐ │
│  │ NotificationService                                     │ │
│  │  └─> envía WhatsApp con emoji según criticality        │ │
│  └────────────────────────────────────────────────────────┘ │
│                                                               │
│  ┌────────────────────────────────────────────────────────┐ │
│  │ JevClient (HTTP wrapper)                                │ │
│  │  ├─> POST a OpenRouter Decisions API                   │ │
│  │  ├─> Retry con backoff exponencial                     │ │
│  │  └─> Validación con Pydantic                           │ │
│  └────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────┘
                             ↓
               ┌─────────────────────────────┐
               │ PostgreSQL + PostGIS         │
               │  - anomalies                 │
               │  - alerts                    │
               │  - jev_classification_cache  │
               └─────────────────────────────┘
```

### Modelo de datos

```python
# backend/app/models/alert.py
from enum import Enum

class AlertCriticality(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"

class Alert(BaseModel):
    id: UUID
    anomaly_id: UUID
    vehicle_id: str
    trip_id: UUID
    criticality: AlertCriticality  # NUEVO
    confidence: float | None  # Score de Jev o None si fue override
    review_required: bool  # True si confidence < threshold
    evidence: dict
    driver_report: str | None
    status: str  # pending, sent, reviewed
    created_at: datetime
    sent_at: datetime | None
```

### API key y configuración

**Variable de entorno** (NO hardcoded):

```bash
JEV_API_KEY=sk-or-v1-...  # OpenRouter API key
JEV_API_ENDPOINT=https://openrouter.ai/api/alpha/decisions
JEV_MODEL=typesafe/jev-1.13.0
JEV_ENABLED=false  # Feature flag, default OFF
JEV_CONFIDENCE_THRESHOLD=0.70
JEV_TIMEOUT_SECONDS=10
JEV_MAX_RETRIES=3
```

**Secret Manager** (producción):

- `JEV_API_KEY` en GCP Secret Manager
- Inyectado como variable de entorno en Cloud Run

## Costos y latencia

### Costo estimado

Con contexto de ~250 tokens por alerta:

- **Costo por clasificación**: ~$0.0000105 (250 tokens × $0.042 / 1M)
- **1,000 alertas**: ~$0.0105 ($0.01)
- **10,000 alertas/mes**: ~$0.105 ($0.11)

**Muy bajo costo**. El cache reduce el costo real aún más (anomalías similares reusan clasificación).

### Latencia

- **Jev API**: 70–500ms
- **Cache hit**: <5ms (lookup en PostgreSQL)
- **Deterministic override**: <1ms
- **Total típico**: 10–100ms (mayoría son cache hits o overrides)

**Impacto**: Mínimo. La clasificación ocurre fuera del path crítico del usuario (antes de enviar WhatsApp).

### Rate limits

OpenRouter (sin plan pago):

- **Requests concurrentes**: Limitado por in-flight budget
- **Estrategia**: Clasificar por lotes con backoff exponencial
- **Para demo**: Sin problema (<100 alertas)

## Política anti-spam por nivel

```python
# Ventanas de throttling por criticidad
ALERT_THROTTLE_WINDOWS = {
    "critical": timedelta(minutes=0),   # Sin throttle, siempre envía
    "high": timedelta(minutes=15),      # Máx 1 alerta cada 15 min
    "medium": timedelta(hours=1),       # Máx 1 alerta por hora
    "low": timedelta(hours=6),          # Máx 1 alerta cada 6 horas
}

def should_send_alert(vehicle_id: str, criticality: str, last_alert_at: datetime | None) -> bool:
    window = ALERT_THROTTLE_WINDOWS[criticality]
    if window == timedelta(0):
        return True  # Critical siempre envía
    
    if not last_alert_at:
        return True
    
    return datetime.now(UTC) - last_alert_at > window
```

## Formato de alerta WhatsApp con criticidad

```python
CRITICALITY_EMOJIS = {
    "critical": "🚨",
    "high": "⚠️",
    "medium": "⚡",
    "low": "ℹ️",
}

def format_alert_message(alert: Alert) -> str:
    emoji = CRITICALITY_EMOJIS[alert.criticality]
    
    if alert.criticality == "critical":
        urgency = "URGENTE - Revisar ahora"
    elif alert.criticality == "high":
        urgency = "Atención requerida"
    elif alert.criticality == "medium":
        urgency = "Monitorear"
    else:
        urgency = "Informativo"
    
    message = f"""{emoji} *{urgency}*

{alert.vehicle_id} · Flete {alert.trip_id}: {alert.evidence['description']}

Consumo: {alert.evidence['observed']} L/100km vs {alert.evidence['baseline']} L/100km (+{alert.evidence['overconsumption_pct']}%)
Ralentí: {alert.evidence['idle_minutes']} min
Tramos afectados: {alert.evidence['segments_affected']}"""

    if alert.driver_report:
        message += f"\n\n💬 Conductor reportó: \"{alert.driver_report}\""
    
    message += f"\n\n🕐 Actualizado {alert.created_at.strftime('%H:%M')}"
    
    return message
```

## Monitoreo y métricas

### Métricas a capturar

```python
# backend/app/metrics/jev_metrics.py
JEV_METRICS = {
    "classifications_total": "Total de clasificaciones Jev",
    "classifications_by_level": "Distribución por criticidad",
    "cache_hit_rate": "% de cache hits vs API calls",
    "deterministic_override_rate": "% de overrides determinísticos",
    "low_confidence_rate": "% de alertas con confidence < threshold",
    "jev_api_latency_ms": "Latencia de API Jev (percentiles)",
    "jev_api_errors": "Errores de API Jev por tipo",
    "jev_cost_usd": "Costo acumulado en USD",
}
```

### Dashboard metrics

- **Tasa de cache hit** (objetivo: >80% después de 1 semana)
- **Distribución de criticidad** (critical/high/medium/low)
- **Confianza promedio** por nivel
- **Items en review queue** (confidence < threshold)
- **Costo diario/mensual**
- **Latencia p50/p95/p99**

## Calibración y tuning

### Proceso de calibración

1. **Recopilar muestra etiquetada** (100–200 anomalías reales del replay FEAD)
2. **Etiquetar manualmente** con gestor de flota real o simulado
3. **Ejecutar clasificación Jev** sobre la muestra
4. **Medir accuracy por banda de confianza**:

```python
def evaluate_sample(labeled_sample: list[LabeledAnomaly]) -> dict:
    bands = [
        ("high_confidence", 0.80, 1.00),
        ("medium_confidence", 0.60, 0.80),
        ("low_confidence", 0.00, 0.60),
    ]
    
    results = {}
    for band_name, min_conf, max_conf in bands:
        items = [s for s in labeled_sample 
                 if min_conf <= s.jev_confidence < max_conf]
        
        correct = sum(1 for s in items 
                     if s.jev_criticality == s.human_label)
        
        results[band_name] = {
            "n": len(items),
            "accuracy": correct / len(items) if items else None,
        }
    
    return results
```

5. **Ajustar threshold de confidence** según accuracy mínima aceptable
6. **Refinar criteria** si un nivel tiene accuracy sistemáticamente baja
7. **Re-testear** hasta alcanzar accuracy objetivo (>85% en high confidence)

### Criterios de ajuste

- Si `critical` tiene muchos falsos positivos → endurecer criteria
- Si `low` se confunde con `medium` → aclarar la diferencia en instructions
- Si confidence general es baja → simplificar opciones (fusionar medium/low)
- Si overlap entre niveles → hacer definitions más exclusivas

## Riesgos y mitigaciones

| Riesgo | Impacto | Probabilidad | Mitigación |
|--------|---------|--------------|------------|
| Jev no disponible (downtime API) | Alertas sin clasificar | Bajo | Fallback: clasificación por reglas determinísticas simples (ej. % sobreconsumo → criticality) |
| Rate limit excedido | Latencia aumentada | Medio | Clasificar por lotes con backoff; cache agresivo; priorizar críticos |
| Clasificación incorrecta (critical vs low) | Falsa urgencia o pérdida de alerta real | Medio | Reglas de override para casos extremos; threshold de confidence; review queue |
| Deriva de modelo entre versiones | Inconsistencia al actualizar | Bajo | Versión fija (`jev-1.13.0`); no auto-update; testing antes de cambiar versión |
| Costo acumulado inesperado | Presupuesto excedido | Muy bajo | Monitoreo de costo; alertas en $X/día; cache permanente reduce costo real |
| Cache stale después de cambio de criteria | Clasificaciones desactualizadas | Medio | Incluir `JEV_QUESTION_VERSION` en cache key; invalidar cache al cambiar criteria |
| Dependencia externa (OpenRouter) | Vendor lock-in | Bajo | Abstracción en `JevClient`; fácil cambio a endpoint directo TypeSafe o self-hosted |

## Preguntas abiertas

1. **¿4 niveles o 3?**: Considerar fusionar `medium` y `low` en un solo nivel "no urgente" si la distinción no es accionable.

2. **¿Clasificación por viaje o por anomalía?**: Actualmente propuesto por anomalía. Alternativa: clasificar el viaje completo según peor anomalía.

3. **¿Re-clasificar si llega reporte del conductor después?**: Si una anomalía clasificada como `high` recibe un reporte del conductor ("esperando descarga"), ¿re-clasificar automáticamente a `medium`? Propuesta: sí, y registrar la transición.

4. **¿Integración con el dashboard?**: Mostrar alertas filtradas por criticidad, con badges de color. Prioridad: P1 (después del MVP).

5. **¿Self-hosted Jev?**: TypeSafe ofrece self-hosting para clientes enterprise. Evaluar si el volumen justifica reducir latencia y costo. Demo: usar OpenRouter.

6. **¿Otros factores de criticidad?**: ¿Incluir valor del flete, tipo de carga (perecibles), retrasos acumulados, historial del conductor? Fase 2.

## Referencias

- [Jev Classifier - TypeSafe AI](https://www.jevtypesafeai.com/jev/classifier)
- [Jev Documentation Hub](https://tryjev.dev/)
- [OpenRouter Jev Classification Cookbook](https://openrouter.ai/docs/cookbook/evaluate-and-optimize/jev-classification)
- [OpenRouter Decisions API Reference](https://openrouter.ai/docs/api-reference/decisions)
- ADR 0002: Línea base por tramo y sentido entre pares
- Issue #14: Línea base y análisis de combustible
- Issue #15: Sobreconsumo, anomalía y alerta registrada
- Product Context: Fleet Intelligence

---

**Versión**: 1.0.0  
**Autor**: Propuesta de integración Jev  
**Fecha**: 2026-10-18  
**Estado**: Draft para revisión
