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

INSERT INTO companies VALUES
    (1, 'Альфа Логистика', 'Логистика'),
    (2, 'Северный Маршрут', 'Логистика'),
    (3, 'Вектор Софт', 'ИТ'),
    (4, 'Облако Системы', 'ИТ'),
    (5, 'Маяк Пром', 'Производство'),
    (6, 'Кварц Завод', 'Производство')
ON CONFLICT DO NOTHING;

INSERT INTO monthly_finance
SELECT id, make_date(y.year, m.month, 1),
    1200000 * id + 170000 * m.month + 200000 * ((id + m.month) % 4)
        + 360000 * (y.year - 2024),
    900000 * id + 130000 * m.month + 180000 * ((id + 2 * m.month) % 5)
        + 240000 * (y.year - 2024),
    2700000 * id - 75000 * m.month - 500000 * (y.year - 2024)
FROM companies
CROSS JOIN generate_series(2024, 2025) AS y(year)
CROSS JOIN generate_series(1, 12) AS m(month)
ON CONFLICT DO NOTHING;

COMMIT;
