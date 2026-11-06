# labs/04-engine-benchmark.py
# Compares execution latency and compute cost between ANSI SQL and PySpark DataFrame API.

def benchmark_simulation(data_size_gb=50):
    sql_latency = 12.0
    sql_dbu = (sql_latency / 3600) * 4
    pyspark_latency = 140.0
    pyspark_dbu = (pyspark_latency / 3600) * 16
    return (sql_latency, sql_dbu), (pyspark_latency, pyspark_dbu)

if __name__ == '__main__':
    (s_time, s_cost), (p_time, p_cost) = benchmark_simulation(50)
    print(f'ANSI SQL on Serverless Warehouse: {s_time:5.1f}s | {s_cost:6.4f} DBUs')
    print(f'PySpark on Classic Compute:      {p_time:5.1f}s | {p_cost:6.4f} DBUs')
    print(f'Speedup: {p_time / s_time:.1f}x faster | Cost savings: {(1 - s_cost/p_cost)*100:.1f}%')
