"""Verifier-owned test packs and drivers (§22 "Verifier melakukan negative/edge/adversarial tests sesuai dampak").

Files here are trusted controller code (protected path ``backend/aeos/verification/**``): the verifier copies
or passes them into its private run directory and executes them against the candidate through
``aeos.verification.runner`` (inside the tamper window). They are never read from a candidate checkout.

* ``probe_api_boundary.py`` - adversarial/negative/edge probes of the platform HTTP boundary (pytest module,
  selected by the ``api``/``auth`` impact categories);
* ``migration_roundtrip.py`` - Alembic upgrade/downgrade/upgrade + seeded backup/restore driver;
* ``openapi_dump.py`` - prints the OpenAPI document of a platform app (contract diff);
* ``ui_driver.py`` - renders a built frontend bundle per UI state in Chromium, screenshots + a11y probe.

The drivers import the verifier's result seal (``aeos_vseal``) before any candidate code and seal their result -
with the digests of the files they wrote - at their very end (``aeos.verification.seal``). The per-candidate
dependency listing is not a driver: the verifier reads the installed metadata itself (``envs.installed_pins``).
"""
