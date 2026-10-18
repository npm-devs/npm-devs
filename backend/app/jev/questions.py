"""Jev question definitions for alert classification."""

JEV_QUESTION_VERSION = "1.0.0"

JEV_CRITICALITY_QUESTION = {
    "version": JEV_QUESTION_VERSION,
    "type": "choice",
    "instructions": "Clasifica la criticidad de esta alerta de sobreconsumo de combustible para el gestor de flota.",
    "criteria": {
        "critical": (
            "Sobreconsumo severo (>40% sobre baseline) SIN explicación del conductor, "
            "o ralentí prolongado >60min sin justificar, o anomalía activa con impacto "
            "económico muy alto. Requiere acción inmediata."
        ),
        "high": (
            "Sobreconsumo significativo (25–40%) sin explicación clara, o ralentí 30–60min "
            "sin justificar, o múltiples tramos afectados (>20km) sin causa conocida. "
            "Requiere revisión pronto."
        ),
        "medium": (
            "Sobreconsumo moderado (15–25%), o explicado por conductor "
            "(ej. 'esperando descarga', 'tráfico'), o ralentí <30min, o patrón no urgente. "
            "Monitoreo requerido."
        ),
        "low": (
            "Sobreconsumo leve (<15%), o un solo tramo aislado, o causa evidente "
            "(pendiente pronunciada, condiciones de ruta), o ya resuelto. Solo registro."
        ),
    },
}


def build_alert_context(
    vehicle_id: str,
    trip_id: str,
    overconsumption_pct: float,
    observed_consumption: float,
    baseline_consumption: float,
    segments_affected: int,
    distance_km: float,
    idle_minutes: float,
    driver_report: str | None,
    trip_status: str,
    remaining_distance_km: float | None = None,
) -> str:
    """
    Build the context string for Jev classification.

    Args:
        vehicle_id: Vehicle identifier (e.g., "T28")
        trip_id: Trip identifier (e.g., "F-104")
        overconsumption_pct: Percentage over baseline (e.g., 30.0 for 30%)
        observed_consumption: Observed consumption in L/100km
        baseline_consumption: Baseline consumption in L/100km
        segments_affected: Number of consecutive segments affected
        distance_km: Total distance affected in km
        idle_minutes: Total idle time detected in minutes
        driver_report: Driver's message, or None if no report
        trip_status: "in_progress" or "completed"
        remaining_distance_km: Remaining distance if in progress

    Returns:
        Context string for Jev state
    """
    context = (
        f"Vehículo {vehicle_id} en flete {trip_id}: "
        f"sobreconsumo de {observed_consumption:.1f} L/100km vs "
        f"{baseline_consumption:.1f} L/100km de línea base "
        f"({overconsumption_pct:.0f}% sobre baseline). "
        f"Afectó {segments_affected} tramo{'s' if segments_affected > 1 else ''} "
        f"consecutivo{'s' if segments_affected > 1 else ''} ({distance_km:.0f} km). "
        f"Ralentí detectado: {idle_minutes:.0f} minutos. "
    )

    if driver_report:
        context += f"Reporte del conductor: {driver_report}. "
    else:
        context += "Reporte del conductor: ninguno. "

    if trip_status == "in_progress":
        if remaining_distance_km is not None:
            context += f"Viaje en progreso, restante: {remaining_distance_km:.0f} km."
        else:
            context += "Viaje en progreso."
    else:
        context += "Viaje completado."

    return context
