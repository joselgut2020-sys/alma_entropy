from typing import Optional


def request_human(reason: str, callback_window: Optional[str] = None):
    """El cliente pidió hablar con una persona, o hay que escalar a un asesor."""
    from data.outcomes_db import init_db, save_outcome

    init_db()

    window = callback_window or "en las próximas 24 horas"

    outcome_id = save_outcome(
        outcome="next_step",
        notes=f"Escalado a asesor humano. Motivo: {reason}. Ventana: {window}.",
    )

    return {
        "status": "success",
        "escalated": True,
        "outcome_id": outcome_id,
        "callback_window": window,
        "message": f"Un asesor se pondrá en contacto {window}.",
    }


def evaluate_options(
    cause: str,
    proposed_date: Optional[str] = None,
    proposed_amount: Optional[float] = None,
):
    options = [
        {
            "option_id": "OP-01",
            "description": "Pago de la cuota completa",
            "amount": 125.00,
            "available": True,
        },
        {
            "option_id": "OP-02",
            "description": "Pago parcial de la cuota",
            "amount": 75.00,
            "available": True,
        },
        {
            "option_id": "OP-03",
            "description": "Reprogramación de pago",
            "amount": 0.00,
            "available": True,
        },
    ]

    return {
        "status": "success",
        "cause": cause,
        "proposed_date": proposed_date,
        "proposed_amount": proposed_amount,
        "options": options,
    }


def validate_proposal(
    date: str,
    amount: Optional[float] = None,
    option_id: Optional[str] = None,
):
    valid_options = {
        "OP-01": 125.00,
        "OP-02": 75.00,
        "OP-03": 0.00,
    }

    if option_id and option_id not in valid_options:
        return {
            "status": "error",
            "valid": False,
            "message": "La opción seleccionada no es válida.",
        }

    if option_id:
        expected_amount = valid_options[option_id]

        if amount is not None and amount != expected_amount:
            return {
                "status": "success",
                "valid": False,
                "message": "El monto no coincide con la opción seleccionada.",
                "expected_amount": expected_amount,
            }

    return {
        "status": "success",
        "valid": True,
        "date": date,
        "amount": amount,
        "option_id": option_id,
        "message": "La propuesta es válida para continuar.",
    }
def register_outcome(
    outcome: str,
    date: Optional[str] = None,
    amount: Optional[float] = None,
    option_id: Optional[str] = None,
    notes: Optional[str] = None,
):
    valid_outcomes = {
        "agreement",
        "refusal",
        "next_step",
    }

    if outcome not in valid_outcomes:
        return {
            "status": "error",
            "registered": False,
            "message": (
                "Resultado inválido. Debe ser "
                "agreement, refusal o next_step."
            ),
        }

    from data.outcomes_db import init_db, save_outcome

    init_db()

    outcome_id = save_outcome(
        outcome=outcome,
        date=date,
        amount=amount,
        option_id=option_id,
        notes=notes,
    )

    return {
        "status": "success",
        "registered": True,
        "outcome_id": outcome_id,
        "outcome": outcome,
        "date": date,
        "amount": amount,
        "option_id": option_id,
        "notes": notes,
        "message": "Resultado registrado correctamente.",
    }