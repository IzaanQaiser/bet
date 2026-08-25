-- Web division Phase 4 — registration completion writes the real `users`
-- row (phone, client-detected timezone, refresh-token secret ref) once
-- OTP verification + Google OAuth consent both succeed. sa-registration
-- never needs UPDATE here — a phone number only ever becomes a user once
-- (the phone_e164 UNIQUE constraint from migrations/0001_init.sql is the
-- real guard against a double-registration attempt).

GRANT SELECT, INSERT ON users TO "sa-registration@obligation-engine-hack.iam";
