BEGIN;

CREATE TABLE IF NOT EXISTS companies (
    id integer PRIMARY KEY,
    name text NOT NULL,
    industry text NOT NULL
);

CREATE TABLE IF NOT EXISTS monthly_finance (
    company_id integer REFERENCES companies(id),
    month date,
    revenue_rub bigint NOT NULL CHECK (revenue_rub >= 0),
    expenses_rub bigint NOT NULL CHECK (expenses_rub >= 0),
    loan_balance_rub bigint NOT NULL CHECK (loan_balance_rub >= 0),
    PRIMARY KEY (company_id, month)
);

COMMIT;
