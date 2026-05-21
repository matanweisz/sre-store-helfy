export const config = {
  port: parseInt(process.env.PORT ?? '4000', 10),
  paymentFailureRate: parseFloat(process.env.PAYMENT_FAILURE_RATE ?? '0.08'),
  paymentLatencyMsMin: parseInt(process.env.PAYMENT_LATENCY_MS_MIN ?? '120', 10),
  paymentLatencyMsMax: parseInt(process.env.PAYMENT_LATENCY_MS_MAX ?? '450', 10),
  mysql: {
    host: process.env.MYSQL_HOST ?? 'localhost',
    port: parseInt(process.env.MYSQL_PORT ?? '3306', 10),
    user: process.env.MYSQL_USER ?? 'shop',
    password: process.env.MYSQL_PASSWORD ?? 'shoppass',
    database: process.env.MYSQL_DATABASE ?? 'shop',
  },
} as const;
