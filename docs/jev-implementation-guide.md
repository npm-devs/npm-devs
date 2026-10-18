# Guía de Implementación: Clasificación de Alertas con Jev

Esta guía proporciona instrucciones paso a paso para implementar y desplegar la clasificación de alertas por criticidad usando Jev en Fleet Intelligence.

## Tabla de contenidos

1. [Pre-requisitos](#pre-requisitos)
2. [Paso 1: Obtener API key de OpenRouter](#paso-1-obtener-api-key-de-openrouter)
3. [Paso 2: Configurar variables de entorno](#paso-2-configurar-variables-de-entorno)
4. [Paso 3: Crear tabla de cache](#paso-3-crear-tabla-de-cache)
5. [Paso 4: Integrar en el flujo de alertas](#paso-4-integrar-en-el-flujo-de-alertas)
6. [Paso 5: Probar con datos de prueba](#paso-5-probar-con-datos-de-prueba)
7. [Paso 6: Calibrar con muestra etiquetada](#paso-6-calibrar-con-muestra-etiquetada)
8. [Paso 7: Ajustar preguntas y criterios](#paso-7-ajustar-preguntas-y-criterios)
9. [Paso 8: Habilitar feature flag](#paso-8-habilitar-feature-flag)
10. [Paso 9: Monitorear en producción](#paso-9-monitorear-en-producción)
11. [Paso 10: Rollback si es necesario](#paso-10-rollback-si-es-necesario)
12. [Troubleshooting](#troubleshooting)

---

## Pre-requisitos

- [ ] Acceso al proyecto en GCP con Cloud SQL y Secret Manager
- [ ] Permisos para crear secrets en Secret Manager
- [ ] Backend desplegado en Cloud Run (issue #10)
- [ ] Anomalías y alertas implementadas (issue #15)
- [ ] Python 3.12+ con `uv` instalado localmente
- [ ] Acceso a la base de datos PostgreSQL (Cloud SQL)

---

## Paso 1: Obtener API key de OpenRouter

### 1.1 Crear cuenta en OpenRouter

1. Ir a https://openrouter.ai/
2. Registrarse o iniciar sesión con cuenta existente
3. Navegar a https://openrouter.ai/keys

### 1.2 Generar API key

1. Click en "Create Key"
2. Nombre sugerido: `fleet-intelligence-jev-prod`
3. Límite de gasto (opcional): $10/mes para empezar
4. Copiar la clave que empieza con `sk-or-v1-...`

⚠️ **IMPORTANTE**: La clave solo se muestra una vez. Guárdala en un lugar seguro.

### 1.3 Agregar créditos (opcional para testing)

OpenRouter ofrece créditos gratuitos para probar. Para producción:

1. Ir a https://openrouter.ai/credits
2. Agregar créditos vía tarjeta de crédito
3. Mínimo recomendado: $5 (suficiente para ~500,000 alertas)

### 1.4 Verificar acceso al modelo

```bash
curl -X POST https://openrouter.ai/api/alpha/decisions \
  -H "Authorization: Bearer sk-or-v1-YOUR_KEY_HERE" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "typesafe/jev-1.13.0",
    "state": {"alert_context": "Test alert"},
    "questions": {
      "test": {
        "type": "choice",
        "instructions": "Is this a test?",
        "criteria": {
          "yes": "This is a test",
          "no": "This is not a test"
        }
      }
    }
  }'
```

Respuesta esperada (status 200):

```json
{
  "model": "typesafe/jev-1.13.0",
  "answers": {
    "test": {
      "type": "choice",
      "choice": "yes",
      ...
    }
  },
  "usage": {...}
}
```

---

## Paso 2: Configurar variables de entorno

### 2.1 Local (desarrollo)

Crear o editar `.env` en `backend/`:

```bash
# Jev configuration
JEV_API_KEY=sk-or-v1-YOUR_KEY_HERE
JEV_API_ENDPOINT=https://openrouter.ai/api/alpha/decisions
JEV_MODEL=typesafe/jev-1.13.0
JEV_ENABLED=false  # Mantener false hasta calibración
JEV_CONFIDENCE_THRESHOLD=0.70
JEV_TIMEOUT_SECONDS=10
JEV_MAX_RETRIES=3
```

### 2.2 GCP Secret Manager (producción)

```bash
# Establecer proyecto
gcloud config set project YOUR_PROJECT_ID

# Crear secret para JEV_API_KEY
echo -n "sk-or-v1-YOUR_KEY_HERE" | gcloud secrets create JEV_API_KEY \
  --data-file=- \
  --replication-policy="automatic"

# Otorgar acceso al service account de Cloud Run
gcloud secrets add-iam-policy-binding JEV_API_KEY \
  --member="serviceAccount:YOUR_SERVICE_ACCOUNT@YOUR_PROJECT.iam.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor"

# Verificar
gcloud secrets versions access latest --secret="JEV_API_KEY"
```

### 2.3 Cloud Run (variables de entorno)

Editar el servicio de Cloud Run para inyectar variables:

```bash
gcloud run services update fleet-backend \
  --region=us-central1 \
  --update-env-vars="\
JEV_API_ENDPOINT=https://openrouter.ai/api/alpha/decisions,\
JEV_MODEL=typesafe/jev-1.13.0,\
JEV_ENABLED=false,\
JEV_CONFIDENCE_THRESHOLD=0.70,\
JEV_TIMEOUT_SECONDS=10,\
JEV_MAX_RETRIES=3" \
  --update-secrets="JEV_API_KEY=JEV_API_KEY:latest"
```

**Nota**: `JEV_ENABLED=false` inicialmente; se habilitará después de calibración.

---

## Paso 3: Crear tabla de cache

### 3.1 Ejecutar migración local

```bash
cd backend
psql $DATABASE_URL < schema/jev_cache.sql
```

### 3.2 Ejecutar migración en Cloud SQL

Opción A - Cloud SQL Proxy:

```bash
# Iniciar proxy
cloud_sql_proxy -instances=YOUR_PROJECT:us-central1:fleet-db=tcp:5432 &

# Aplicar schema
psql "host=localhost port=5432 dbname=fleetdb user=postgres" < schema/jev_cache.sql
```

Opción B - Cloud Shell:

```bash
gcloud sql connect fleet-db --user=postgres --quiet < schema/jev_cache.sql
```

### 3.3 Verificar tabla creada

```sql
-- Conectar a la BD
psql $DATABASE_URL

-- Verificar tabla
\d jev_classification_cache

-- Debe mostrar:
-- - Columnas: cache_key, criticality, confidence, probabilities, etc.
-- - Constraints: criticality_valid, source_valid
-- - Indexes: idx_jev_cache_anomaly, idx_jev_cache_classified_at, etc.

-- Salir
\q
```

---

## Paso 4: Integrar en el flujo de alertas

### 4.1 Ubicar el punto de integración

En el backend actual (issue #15), el flujo es:

```
AnomalyDetector → genera Anomaly → AlertGenerator → Alert → NotificationService
```

**Punto de integración**: Entre `Anomaly` y `Alert`, agregar clasificación.

### 4.2 Modificar AlertGenerator

Editar `backend/app/alerts/generator.py` (o el archivo equivalente según implementación):

```python
from app.jev import AlertClassifier, AnomalyEvidence
from app.jev.cache import PostgresCacheStore
from app.jev.config import JevConfig

# En la clase AlertGenerator:

def __init__(self, database_url: str):
    self.database_url = database_url
    
    # Inicializar Jev classifier
    try:
        jev_config = JevConfig.from_env()
        cache_store = PostgresCacheStore(database_url)
        self.jev_classifier = AlertClassifier(jev_config, cache_store)
    except ValueError as exc:
        # JEV_API_KEY no configurada
        logger.warning("Jev not configured: %s. Using fallback only.", exc)
        self.jev_classifier = None

async def generate_alert(self, anomaly: Anomaly) -> Alert:
    """Generate alert from anomaly with Jev classification."""
    
    # 1. Construir evidencia para Jev
    evidence = AnomalyEvidence(
        vehicle_id=anomaly.vehicle_id,
        trip_id=anomaly.trip_id,
        overconsumption_pct=anomaly.overconsumption_pct,
        observed_consumption=anomaly.observed_consumption,
        baseline_consumption=anomaly.baseline_consumption,
        segments_affected=anomaly.segments_affected,
        distance_km=anomaly.distance_km,
        idle_minutes=anomaly.idle_minutes,
        driver_report=anomaly.driver_report,
        trip_status=anomaly.trip_status,
        remaining_distance_km=anomaly.remaining_distance_km,
    )
    
    # 2. Clasificar criticidad
    if self.jev_classifier:
        classification = await self.jev_classifier.classify(evidence)
        criticality = classification.criticality
        confidence = classification.confidence
        review_required = classification.review_required
    else:
        # Sin Jev, usar fallback simple
        criticality = self._simple_fallback(anomaly)
        confidence = None
        review_required = True
    
    # 3. Crear alerta con criticidad
    alert = Alert(
        id=uuid4(),
        anomaly_id=anomaly.id,
        vehicle_id=anomaly.vehicle_id,
        trip_id=anomaly.trip_id,
        criticality=criticality,
        confidence=confidence,
        review_required=review_required,
        evidence=anomaly.to_dict(),
        driver_report=anomaly.driver_report,
        status="pending",
        created_at=datetime.now(UTC),
    )
    
    # 4. Aplicar política anti-spam según criticidad
    if not self._should_send(alert):
        alert.status = "throttled"
    
    return alert
```

### 4.3 Actualizar modelo Alert

Editar `backend/app/models/alert.py`:

```python
from app.jev import AlertCriticality

class Alert(BaseModel):
    id: UUID
    anomaly_id: UUID
    vehicle_id: str
    trip_id: UUID
    criticality: AlertCriticality  # NUEVO
    confidence: float | None  # NUEVO
    review_required: bool  # NUEVO
    evidence: dict
    driver_report: str | None
    status: str  # pending, throttled, sent, reviewed
    created_at: datetime
    sent_at: datetime | None
```

### 4.4 Actualizar schema de base de datos

```sql
-- Agregar columnas a tabla alerts (si existe)
ALTER TABLE alerts 
  ADD COLUMN criticality TEXT NOT NULL DEFAULT 'medium',
  ADD COLUMN confidence NUMERIC(5,4),
  ADD COLUMN review_required BOOLEAN NOT NULL DEFAULT false;

-- Constraint
ALTER TABLE alerts 
  ADD CONSTRAINT criticality_valid 
  CHECK (criticality IN ('critical', 'high', 'medium', 'low'));
```

---

## Paso 5: Probar con datos de prueba

### 5.1 Test unitario

```bash
cd backend
uv run pytest tests/test_jev_classifier.py -v
```

Todos los tests deben pasar:

- `test_deterministic_override_critical_high_overconsumption`
- `test_deterministic_override_low_minimal_overconsumption`
- `test_fallback_classification_critical`
- `test_cache_hit`
- etc.

### 5.2 Test de integración con mock

Crear `backend/tests/test_alert_generation_with_jev.py`:

```python
import pytest
from app.alerts.generator import AlertGenerator
from app.models.anomaly import Anomaly

@pytest.mark.asyncio
async def test_alert_generation_with_jev_disabled():
    """Test alert generation when Jev is disabled (fallback mode)."""
    generator = AlertGenerator(database_url=os.environ["DATABASE_URL"])
    
    anomaly = Anomaly(
        id=uuid4(),
        vehicle_id="T28",
        trip_id=uuid4(),
        overconsumption_pct=45.0,
        observed_consumption=46.4,
        baseline_consumption=32.0,
        segments_affected=3,
        distance_km=30.0,
        idle_minutes=40.0,
        driver_report=None,
        trip_status="in_progress",
    )
    
    alert = await generator.generate_alert(anomaly)
    
    assert alert.criticality in ["critical", "high", "medium", "low"]
    assert alert.review_required is True  # Fallback always requires review
```

### 5.3 Test manual con curl

**Pre-requisito**: Endpoint de creación de alerta expuesto (o crear script de test).

```bash
# Simular anomalía crítica
curl -X POST http://localhost:8000/test/simulate-alert \
  -H "Content-Type: application/json" \
  -d '{
    "vehicle_id": "T28",
    "trip_id": "test-trip-001",
    "overconsumption_pct": 52.0,
    "idle_minutes": 10.0,
    "driver_report": null,
    "trip_status": "in_progress"
  }'

# Verificar en cache
psql $DATABASE_URL -c "SELECT cache_key, criticality, confidence, source FROM jev_classification_cache ORDER BY classified_at DESC LIMIT 5;"
```

Resultado esperado:

- `criticality`: `critical` (por override determinístico, >50%)
- `source`: `deterministic_override`
- `confidence`: NULL

---

## Paso 6: Calibrar con muestra etiquetada

### 6.1 Recopilar muestra de anomalías reales

Ejecutar el replay FEAD (días 13–19) y exportar 100–200 anomalías:

```sql
-- Exportar anomalías del replay a CSV
COPY (
  SELECT 
    vehicle_id, 
    trip_id, 
    overconsumption_pct, 
    observed_consumption, 
    baseline_consumption, 
    segments_affected, 
    distance_km, 
    idle_minutes, 
    driver_report, 
    trip_status
  FROM anomalies
  WHERE created_at >= '2026-10-19'  -- Replay de demo
  LIMIT 150
) TO '/tmp/anomalies_sample.csv' WITH CSV HEADER;
```

### 6.2 Etiquetar manualmente

Crear `calibration/label_sample.py`:

```python
import csv

def label_anomaly(row: dict) -> str:
    """Manual labeling helper."""
    print("\n" + "="*60)
    print(f"Vehículo: {row['vehicle_id']}")
    print(f"Sobreconsumo: {row['overconsumption_pct']}%")
    print(f"Observado: {row['observed_consumption']} L/100km")
    print(f"Baseline: {row['baseline_consumption']} L/100km")
    print(f"Tramos: {row['segments_affected']}")
    print(f"Ralentí: {row['idle_minutes']} min")
    print(f"Reporte conductor: {row['driver_report'] or 'ninguno'}")
    print(f"Estado viaje: {row['trip_status']}")
    print("="*60)
    
    while True:
        label = input("Criticidad (1=critical, 2=high, 3=medium, 4=low): ")
        if label in ["1", "2", "3", "4"]:
            return ["critical", "high", "medium", "low"][int(label) - 1]
        print("Opción inválida. Intenta de nuevo.")

# Cargar CSV y etiquetar
with open('/tmp/anomalies_sample.csv') as f:
    reader = csv.DictReader(f)
    labeled = []
    
    for row in reader:
        label = label_anomaly(row)
        row['human_label'] = label
        labeled.append(row)

# Guardar etiquetado
with open('calibration/labeled_sample.csv', 'w', newline='') as f:
    writer = csv.DictWriter(f, fieldnames=labeled[0].keys())
    writer.writeheader()
    writer.writerows(labeled)

print(f"\n✓ {len(labeled)} anomalías etiquetadas guardadas en calibration/labeled_sample.csv")
```

Ejecutar:

```bash
python calibration/label_sample.py
```

**Consejo**: Involucrar al gestor de flota real (si existe) o simular su criterio.

### 6.3 Clasificar la muestra con Jev

Habilitar Jev temporalmente:

```bash
export JEV_ENABLED=true
```

Ejecutar script de clasificación:

```python
# calibration/classify_sample.py
import asyncio
import csv
from app.jev import AlertClassifier, AnomalyEvidence
from app.jev.cache import InMemoryCacheStore
from app.jev.config import JevConfig

async def classify_sample():
    config = JevConfig.from_env()
    config.enabled = True  # Forzar habilitación
    cache = InMemoryCacheStore()
    classifier = AlertClassifier(config, cache)
    
    with open('calibration/labeled_sample.csv') as f:
        reader = csv.DictReader(f)
        results = []
        
        for row in reader:
            evidence = AnomalyEvidence(
                vehicle_id=row['vehicle_id'],
                trip_id=row['trip_id'],
                overconsumption_pct=float(row['overconsumption_pct']),
                observed_consumption=float(row['observed_consumption']),
                baseline_consumption=float(row['baseline_consumption']),
                segments_affected=int(row['segments_affected']),
                distance_km=float(row['distance_km']),
                idle_minutes=float(row['idle_minutes']),
                driver_report=row['driver_report'] if row['driver_report'] else None,
                trip_status=row['trip_status'],
            )
            
            classification = await classifier.classify(evidence)
            
            results.append({
                **row,
                'jev_criticality': classification.criticality.value,
                'jev_confidence': classification.confidence,
                'jev_source': classification.source,
            })
            
            print(f"Clasificado {row['vehicle_id']}: {classification.criticality.value} (conf={classification.confidence:.2f if classification.confidence else 'N/A'})")
    
    # Guardar resultados
    with open('calibration/classified_sample.csv', 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=results[0].keys())
        writer.writeheader()
        writer.writerows(results)
    
    print(f"\n✓ {len(results)} clasificaciones guardadas en calibration/classified_sample.csv")

asyncio.run(classify_sample())
```

Ejecutar:

```bash
python calibration/classify_sample.py
```

### 6.4 Medir accuracy por banda de confianza

```python
# calibration/evaluate.py
import csv
from collections import defaultdict

def evaluate():
    with open('calibration/classified_sample.csv') as f:
        reader = csv.DictReader(f)
        data = list(reader)
    
    # Bandas de confianza
    bands = [
        ("high", 0.80, 1.00),
        ("medium", 0.60, 0.80),
        ("low", 0.00, 0.60),
    ]
    
    for band_name, min_conf, max_conf in bands:
        items = [
            d for d in data 
            if d['jev_confidence'] and float(d['jev_confidence']) >= min_conf and float(d['jev_confidence']) < max_conf
        ]
        
        if not items:
            print(f"\nBanda {band_name} ({min_conf}–{max_conf}): sin items")
            continue
        
        correct = sum(1 for d in items if d['jev_criticality'] == d['human_label'])
        accuracy = correct / len(items)
        
        print(f"\nBanda {band_name} ({min_conf}–{max_conf}):")
        print(f"  Items: {len(items)}")
        print(f"  Correctos: {correct}")
        print(f"  Accuracy: {accuracy:.2%}")
    
    # Accuracy global
    correct_total = sum(1 for d in data if d['jev_criticality'] == d['human_label'])
    accuracy_total = correct_total / len(data)
    print(f"\nAccuracy total: {accuracy_total:.2%} ({correct_total}/{len(data)})")

evaluate()
```

Ejecutar:

```bash
python calibration/evaluate.py
```

**Objetivo**: Accuracy >85% en banda de alta confianza (>0.80).

---

## Paso 7: Ajustar preguntas y criterios

Si la accuracy no alcanza el objetivo:

### 7.1 Analizar confusión entre niveles

```python
# calibration/confusion_matrix.py
import csv
from collections import defaultdict

with open('calibration/classified_sample.csv') as f:
    reader = csv.DictReader(f)
    data = list(reader)

confusion = defaultdict(lambda: defaultdict(int))

for d in data:
    human = d['human_label']
    jev = d['jev_criticality']
    confusion[human][jev] += 1

print("\nMatriz de confusión (filas=humano, columnas=Jev):")
print(f"{'':12} {'critical':>10} {'high':>10} {'medium':>10} {'low':>10}")
for human_label in ['critical', 'high', 'medium', 'low']:
    row = [f"{human_label:12}"]
    for jev_label in ['critical', 'high', 'medium', 'low']:
        count = confusion[human_label][jev_label]
        row.append(f"{count:10}")
    print("".join(row))
```

### 7.2 Refinar criterios según patrones

**Ejemplo**: Si `high` se confunde mucho con `medium`:

```python
# Antes (en backend/app/jev/questions.py):
"high": "Sobreconsumo significativo (25–40%)..."

# Después - endurecer el umbral:
"high": "Sobreconsumo significativo (30–40%) sin explicación clara, 
         o ralentí 40–60min sin justificar, o >3 tramos afectados sin causa. 
         Requiere revisión en <1 hora."
```

**Incrementar versión** para invalidar cache:

```python
JEV_QUESTION_VERSION = "1.1.0"  # Era 1.0.0
```

### 7.3 Re-clasificar y re-evaluar

```bash
# Limpiar cache antiguo
psql $DATABASE_URL -c "DELETE FROM jev_classification_cache WHERE request_context->>'question_version' = '1.0.0';"

# Re-clasificar muestra con nueva versión
python calibration/classify_sample.py

# Re-evaluar
python calibration/evaluate.py
```

Repetir hasta alcanzar accuracy objetivo.

### 7.4 Simplificar niveles si es necesario

Si es difícil distinguir entre `medium` y `low`, considerar fusionarlos:

```python
# Reducir a 3 niveles: critical, high, low
"critical": "...",
"high": "...",
"low": "Sobreconsumo leve o moderado (<30%), o causa conocida, o viaje completado. 
         Monitoreo o solo registro."
```

---

## Paso 8: Habilitar feature flag

### 8.1 Verificar calibración exitosa

- [ ] Accuracy >85% en banda high confidence
- [ ] No hay confusiones graves (ej. `critical` clasificado como `low`)
- [ ] Costo promedio por clasificación <$0.00003 (30 tokens × $0.042/1M)

### 8.2 Habilitar en staging

```bash
# Local
export JEV_ENABLED=true

# Cloud Run (staging)
gcloud run services update fleet-backend-staging \
  --region=us-central1 \
  --update-env-vars="JEV_ENABLED=true"
```

### 8.3 Monitorear primeras 100 alertas

```sql
-- Ver distribución de criticidad
SELECT 
  criticality, 
  COUNT(*) as count,
  AVG(confidence) as avg_confidence,
  COUNT(*) FILTER (WHERE review_required) as review_count
FROM alerts
WHERE created_at > NOW() - INTERVAL '1 hour'
GROUP BY criticality;

-- Ver fuentes de clasificación
SELECT 
  c.source,
  COUNT(*) as count,
  AVG(c.cost_usd) as avg_cost
FROM jev_classification_cache c
WHERE c.classified_at > NOW() - INTERVAL '1 hour'
GROUP BY c.source;
```

**Esperado**:

- Distribución: ~5–10% `critical`, ~20–30% `high`, ~40–50% `medium`, ~20–30% `low`
- Cache hit rate: >50% después de 1 hora
- Costo promedio: <$0.00003 por clasificación

### 8.4 Habilitar en producción

```bash
gcloud run services update fleet-backend \
  --region=us-central1 \
  --update-env-vars="JEV_ENABLED=true"
```

---

## Paso 9: Monitorear en producción

### 9.1 Métricas clave

Crear dashboard en GCP Monitoring o Grafana:

```sql
-- Query 1: Distribución de criticidad (últimas 24h)
SELECT 
  criticality,
  COUNT(*) as count,
  ROUND(COUNT(*) * 100.0 / SUM(COUNT(*)) OVER (), 1) as percentage
FROM alerts
WHERE created_at > NOW() - INTERVAL '24 hours'
GROUP BY criticality
ORDER BY 
  CASE criticality
    WHEN 'critical' THEN 1
    WHEN 'high' THEN 2
    WHEN 'medium' THEN 3
    WHEN 'low' THEN 4
  END;

-- Query 2: Cache hit rate
SELECT 
  source,
  COUNT(*) as count,
  ROUND(COUNT(*) * 100.0 / SUM(COUNT(*)) OVER (), 1) as percentage
FROM jev_classification_cache
WHERE classified_at > NOW() - INTERVAL '24 hours'
GROUP BY source;

-- Query 3: Alertas que requieren revisión
SELECT COUNT(*) as review_queue
FROM alerts
WHERE review_required = true 
  AND status NOT IN ('reviewed', 'dismissed')
  AND created_at > NOW() - INTERVAL '7 days';

-- Query 4: Costo diario
SELECT 
  DATE(classified_at) as date,
  COUNT(*) as classifications,
  ROUND(SUM(cost_usd)::numeric, 6) as cost_usd
FROM jev_classification_cache
WHERE cost_usd IS NOT NULL
  AND classified_at > NOW() - INTERVAL '30 days'
GROUP BY DATE(classified_at)
ORDER BY date DESC;

-- Query 5: Latencia promedio (requiere logging adicional)
-- Implementar en backend: registrar tiempo de clasificación
```

### 9.2 Alertas automáticas

Configurar alertas en GCP Monitoring:

**Alerta 1: Review queue creciendo**

```
Condition: review_queue > 50
Duration: 1 hour
Action: Email to ops team
```

**Alerta 2: Costo diario excedido**

```
Condition: daily_cost_usd > $0.50
Duration: 1 day
Action: Email to product owner
```

**Alerta 3: Alta tasa de errores de Jev API**

```
Condition: error_rate > 5%
Duration: 10 minutes
Action: PagerDuty (if critical)
```

### 9.3 Revisión semanal

Cada semana:

1. **Exportar alertas de baja confianza** para revisión humana:

```sql
COPY (
  SELECT a.vehicle_id, a.trip_id, a.criticality, a.confidence, a.evidence
  FROM alerts a
  WHERE a.review_required = true 
    AND a.created_at > NOW() - INTERVAL '7 days'
  ORDER BY a.created_at DESC
) TO '/tmp/alerts_to_review.csv' WITH CSV HEADER;
```

2. **Etiquetar manualmente** casos de baja confianza
3. **Comparar con clasificación Jev** para detectar errores sistemáticos
4. **Ajustar criterios** si se detectan patrones de error (volver a paso 7)

---

## Paso 10: Rollback si es necesario

### 10.1 Desactivar Jev inmediatamente

```bash
# Cloud Run
gcloud run services update fleet-backend \
  --region=us-central1 \
  --update-env-vars="JEV_ENABLED=false"

# Verifica que el cambio se aplicó
gcloud run services describe fleet-backend \
  --region=us-central1 \
  --format="value(spec.template.spec.containers[0].env)"
```

Con `JEV_ENABLED=false`, el sistema usa **fallback determinístico** automáticamente.

### 10.2 Verificar que fallback funciona

```bash
# Simular alerta
curl -X POST https://fleet-backend.run.app/test/simulate-alert \
  -H "Content-Type: application/json" \
  -d '{
    "vehicle_id": "T99",
    "overconsumption_pct": 35.0,
    "trip_status": "in_progress"
  }'

# Verificar en logs que se usó fallback
gcloud logging read "resource.type=cloud_run_revision AND textPayload:'Using fallback rule-based classification'" --limit=5
```

### 10.3 Investigar causa del rollback

Casos comunes:

1. **Alta tasa de errores 402 (créditos agotados)**:
   - Solución: Agregar créditos en OpenRouter
   - Verificar: `gcloud logging read "textPayload:'402'" --limit=50`

2. **Clasificaciones incorrectas sistemáticas**:
   - Solución: Revisar criterios en `questions.py`, recalibrar
   - Verificar: Comparar con muestra etiquetada

3. **Latencia alta (>2s)**:
   - Solución: Revisar timeout, aumentar cache hit rate
   - Verificar: Logs de latencia en Cloud Run

4. **Costo inesperadamente alto**:
   - Solución: Verificar que cache funciona, revisar rate de cache miss
   - Verificar: Query de costo diario (paso 9.1)

### 10.4 Re-habilitar después de fix

1. Aplicar fix (ej. agregar créditos, ajustar criterios)
2. Probar en staging primero
3. Monitorear métricas de staging por 1 hora
4. Si todo bien, re-habilitar en producción

---

## Troubleshooting

### Problema: "JEV_API_KEY environment variable is required"

**Causa**: Variable no configurada.

**Solución**:

```bash
# Local
export JEV_API_KEY=sk-or-v1-...

# Cloud Run
gcloud secrets create JEV_API_KEY --data-file=- < jev_key.txt
gcloud run services update fleet-backend --update-secrets="JEV_API_KEY=JEV_API_KEY:latest"
```

### Problema: "Jev API error 401: Unauthorized"

**Causa**: API key inválida o expirada.

**Solución**:

1. Verificar que la key es correcta (empieza con `sk-or-v1-`)
2. Ir a https://openrouter.ai/keys y verificar que la key existe
3. Regenerar key si es necesario y actualizar secret

### Problema: "Jev API error 402: Insufficient credits"

**Causa**: Créditos agotados en OpenRouter.

**Solución**:

1. Ir a https://openrouter.ai/credits
2. Agregar créditos
3. El sistema reintentará automáticamente

### Problema: "Jev API error 429: Rate limit exceeded"

**Causa**: Demasiadas requests concurrentes.

**Solución**:

El cliente ya maneja 429 con retry automático. Si persiste:

1. Reducir concurrencia de alertas
2. Aumentar cache hit rate
3. Considerar plan pago de OpenRouter con rate limits más altos

### Problema: Cache hit rate muy bajo (<30%)

**Causa**: Anomalías muy diferentes entre sí, o cache key muy sensible.

**Solución**:

1. Verificar normalización de valores numéricos en `_build_cache_key`
2. Considerar "buckets" de valores (ej. redondear overconsumption_pct a 5%)
3. Aceptar hit rate menor si las anomalías son genuinamente distintas

### Problema: Accuracy baja (<70%) en high confidence

**Causa**: Criterios no bien definidos o muestra etiquetada inconsistente.

**Solución**:

1. Re-etiquetar muestra con 2 personas y medir acuerdo inter-rater
2. Simplificar niveles (de 4 a 3)
3. Hacer criterios más explícitos con ejemplos
4. Aumentar tamaño de muestra de calibración (200–500 items)

### Problema: Muchas alertas en review queue

**Causa**: Threshold de confianza demasiado alto o criterios ambiguos.

**Solución**:

1. Reducir `JEV_CONFIDENCE_THRESHOLD` de 0.70 a 0.60
2. Ajustar criteria para que sean más distintos entre sí
3. Aceptar que algunos casos borderline siempre requerirán revisión

### Problema: Clasificaciones flipean entre ejecuciones

**Causa**: Jev no es determinístico; caso borderline.

**Solución**:

1. Verificar que cache funciona (mismo input → mismo output)
2. Para casos borderline, confidence será baja → irán a review
3. Si un caso flip, agregarlo a la muestra y ajustar criterios

---

## Recursos adicionales

- [Jev Documentation Hub](https://tryjev.dev/)
- [OpenRouter Jev Cookbook](https://openrouter.ai/docs/cookbook/evaluate-and-optimize/jev-classification)
- [OpenRouter Decisions API Reference](https://openrouter.ai/docs/api-reference/decisions)
- [TypeSafe AI Support](mailto:support@typesafe.ai)
- Issue #15: Sobreconsumo, anomalía y alerta registrada
- `docs/jev-alarm-classification.md`: Design doc completo

---

**Versión**: 1.0.0  
**Última actualización**: 2026-10-18  
**Mantenedor**: Team Fleet Intelligence
