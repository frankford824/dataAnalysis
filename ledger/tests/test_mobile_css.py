from pathlib import Path


def test_phone_store_editor_drops_fixed_five_column_grid():
    css = (Path(__file__).parents[1] / "ledger/static/app.css").read_text(encoding="utf-8")
    phone = css.split("@media (max-width: 640px)", 1)[1]
    assert ".storerow { grid-template-columns: minmax(0, 1fr)" in phone
    assert "table.deliver, table.cols" in phone
    assert "overflow-x: auto" in phone
