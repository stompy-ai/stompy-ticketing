"""Named decision reverts for STOMPY-3777, on disposable local PG only.

Reuses 462's isolated pytest/XML runner; its harness/reverts remain unchanged.
"""

import os
from pathlib import Path

import red_462_ticket_grep as runner

ROOT = Path(__file__).resolve().parents[2]
A = "tests/test_ticket_answers_3777.py::"
Q = "tests/test_ticket_card_query_3777.py::"
N = "tests/test_ticket_card_native_3777.py::"
FILES = [prefix[:-2] for prefix in (A, Q, N)]
CONTROLS = {
    A + "test_numeric_token_remains_exact_independently_of_display",
    A + "test_strong_duplicate_guidance_and_candidates_are_preserved",
    N + "test_full_reader_remains_complete_without_card_projection",
    N + "test_native_card_preserves_new_optional_columns",
}
REVERTS = [
    (
        "rewrite_user_metadata_as_ticket",
        "stompy_ticketing/mcp_tools.py",
        'present=present and key != "metadata"',
        "present=present",
        {A + "test_user_metadata_is_not_rewritten_as_a_card"},
    ),
    (
        "fetch_the_whole_body",
        "stompy_ticketing/ticket_listing.py",
        '(sql.SQL("*"), []) if fields == "full"',
        '(sql.SQL("*"), []) if True',
        {Q + "test_card_query_projects_preview_without_the_complete_body"}
        | {
            N
            + "test_native_card_matches_old_preview_and_preserves_full_get["
            + case
            + "]"
            for case in (
                "null",
                "empty",
                "100unicode",
                "103unicode",
                "104unicode",
                "100k",
            )
        },
    ),
    (
        "count_only_the_page",
        "stompy_ticketing/ticket_listing.py",
        "total = sum(by_status.values())",
        "total = len(rows)",
        {
            Q + "test_total_is_derived_from_filtered_status_counts",
            N + "test_recreated_legacy_table_does_not_reuse_old_optional_columns",
        },
    ),
    (
        "echo_create_body",
        "stompy_ticketing/mcp_tools.py",
        '"status": "created",\n                        "ticket": _card_with_size(_decorate(result)),',
        '"status": "created",\n                        "ticket": _decorate(result).model_dump(),',
        {A + "test_create_receipt_is_a_card_with_size_and_action"},
    ),
    (
        "echo_update_body",
        "stompy_ticketing/mcp_tools.py",
        'return _safe_json({"status": "updated", "ticket": _card_with_size(_decorate(result))})',
        'return _safe_json({"status": "updated", "ticket": _decorate(result).model_dump()})',
        {A + "test_update_receipt_is_a_card_with_size_and_action"},
    ),
    (
        "bare_title_error",
        "stompy_ticketing/mcp_tools.py",
        "mcp_error('TITLE_REQUIRED', 'title is required for create')",
        '_toon_encode({"error": "title is required for create"})',
        {
            A + "test_missing_title_uses_house_refusal_shape",
            A + "test_refusals_use_the_same_default_toon_format_as_reads",
        },
    ),
    (
        "no_readable_time",
        "stompy_ticketing/ticket_answers.py",
        'for name in ("created_at", "updated_at", "closed_at"):',
        "for name in ():",
        {A + "test_readable_time_does_not_round_the_concurrency_token"},
    ),
    (
        "no_allowed_next",
        "stompy_ticketing/mcp_tools.py",
        '"allowed_next": getattr(e, "allowed_next", [])',
        '"allowed_next": []',
        {
            A + "test_invalid_transition_carries_allowed_next_as_data",
            A + "test_close_refusal_names_terminal_choices_instead_of_repeating_close",
        },
    ),
    (
        "weak_match_is_a_close_instruction",
        "stompy_ticketing/duplicates.py",
        'GUIDANCE if any(float(hint.get("similarity", 0)) >= 0.5 for hint in hints)',
        "GUIDANCE if True",
        {A + "test_weak_duplicate_hint_is_advisory_without_close_instruction"},
    ),
    (
        "trust_recreated_column_cache",
        "stompy_ticketing/ticket_projection.py",
        "_ticket_columns(conn, cur, schema, fresh=True)",
        "_ticket_columns(conn, cur, schema)",
        {N + "test_recreated_legacy_table_does_not_reuse_old_optional_columns"},
    ),
    (
        "echo_search_arguments",
        "stompy_ticketing/ticket_answers.py",
        'if isinstance(data, dict) and "tickets" in data and "query" in data:',
        'if False and isinstance(data, dict) and "tickets" in data and "query" in data:',
        {A + "test_search_keeps_zero_result_but_omits_argument_echoes"},
    ),
    (
        "ambiguous_outcome_key",
        "stompy_ticketing/ticket_answers.py",
        'data["action"] = data.pop("status")',
        'data["status"] = data.pop("status")',
        {
            A + "test_create_receipt_is_a_card_with_size_and_action",
            A + "test_update_receipt_is_a_card_with_size_and_action",
        },
    ),
    (
        "raw_author_id_on_card",
        "stompy_ticketing/ticket_answers.py",
        'data.pop("created_by", None)',
        'data.get("created_by")',
        {A + "test_create_receipt_is_a_card_with_size_and_action"},
    ),
    (
        "skip_roundtrip_validation",
        "stompy_ticketing/errors.py",
        "if decode(encoded) == data:",
        "if True:",
        {A + "test_refusal_recovery_steps_survive_codec_roundtrip_failure"},
    ),
    (
        "json_refusals_by_default",
        "stompy_ticketing/errors.py",
        "return encode_answer(payload)\n\n\ndef recoverable_error(",
        "return json.dumps(payload, indent=2)\n\n\ndef recoverable_error(",
        {
            A + "test_missing_title_uses_house_refusal_shape",
            A + "test_close_refusal_names_terminal_choices_instead_of_repeating_close",
            A + "test_invalid_transition_carries_allowed_next_as_data",
            A + "test_refusals_use_the_same_default_toon_format_as_reads",
        },
    ),
]


def main():
    assert os.environ.get("STOMPY_462_LOCAL_PG"), "start disposable local PG first"
    assert runner.ROOT == ROOT
    # Configure the shared runner only in this process. The 462 source,
    # mutations, expected sets and controls remain byte-identical on disk.
    runner.FILES = FILES
    runner.CONTROLS = CONTROLS
    runner.REVERTS = REVERTS
    return runner.main()


if __name__ == "__main__":
    raise SystemExit(main())
