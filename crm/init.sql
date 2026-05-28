CREATE TABLE IF NOT EXISTS public.crm_clients (
    user_id TEXT NOT NULL,
    client_name TEXT NOT NULL,
    email TEXT NOT NULL,
    prosthesis_id TEXT NOT NULL,
    updated_at TIMESTAMP NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, prosthesis_id)
);

INSERT INTO public.crm_clients (user_id, client_name, email, prosthesis_id, updated_at)
VALUES
    ('user1', 'Ivan Petrov', 'user1@example.com', 'prosthesis-001', now()),
    ('user2', 'Anna Smirnova', 'user2@example.com', 'prosthesis-002', now()),
    ('admin1', 'Admin User', 'admin1@example.com', 'prosthesis-003', now())
ON CONFLICT (user_id, prosthesis_id) DO UPDATE
SET
    client_name = EXCLUDED.client_name,
    email = EXCLUDED.email,
    updated_at = EXCLUDED.updated_at;
