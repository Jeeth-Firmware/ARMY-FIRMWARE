#!/usr/bin/env python3
"""
Stress Test Script for API: https://api.copilotai.click/api/store-dc-data
Sends 500 concurrent requests to test API performance.
"""

import asyncio
import aiohttp
import time
import random
import argparse
from datetime import datetime
from dataclasses import dataclass
from typing import List


@dataclass
class TestResult:
    success: bool
    status_code: int
    response_time: float
    error: str = ""


class StressTest:
    def __init__(
        self,
        url: str,
        bearer_token: str,
        device_id: str,
        concurrent_requests: int = 500,
        total_requests: int = 500,
    ):
        self.url = url
        self.bearer_token = bearer_token
        self.device_id = device_id
        self.concurrent_requests = concurrent_requests
        self.total_requests = total_requests
        self.results: List[TestResult] = []

    def generate_sample_data(self) -> dict:
        """Generate sample GPS data payload."""
        return {
            "device_id": self.device_id,
            "data": [
                {
                    "lat": round(random.uniform(10.0, 11.0), 6),
                    "long": round(random.uniform(106.0, 107.0), 6),
                    "speed": round(random.uniform(0, 120), 2),
                    "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "driver_status": random.choice(["normal", "drowsy", "distracted"]),
                    "acceleration": round(random.uniform(-5, 5), 2),
                }
            ],
        }

    async def send_request(self, session: aiohttp.ClientSession, request_id: int) -> TestResult:
        """Send a single request and return the result."""
        headers = {
            "Authorization": f"Bearer {self.bearer_token}",
            "Content-Type": "application/json",
        }
        payload = self.generate_sample_data()
        start_time = time.perf_counter()

        try:
            async with session.post(
                self.url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=30)
            ) as response:
                response_time = time.perf_counter() - start_time
                await response.text()
                return TestResult(
                    success=response.status in [200, 201],
                    status_code=response.status,
                    response_time=response_time,
                )
        except asyncio.TimeoutError:
            return TestResult(
                success=False,
                status_code=0,
                response_time=time.perf_counter() - start_time,
                error="Timeout",
            )
        except Exception as e:
            return TestResult(
                success=False,
                status_code=0,
                response_time=time.perf_counter() - start_time,
                error=str(e),
            )

    async def run_batch(self, session: aiohttp.ClientSession, start_id: int, batch_size: int):
        """Run a batch of concurrent requests."""
        tasks = [
            self.send_request(session, start_id + i)
            for i in range(batch_size)
        ]
        return await asyncio.gather(*tasks)

    async def run(self):
        """Run the stress test."""
        print(f"\n{'='*60}")
        print(f"STRESS TEST - {self.url}")
        print(f"{'='*60}")
        print(f"Concurrent requests: {self.concurrent_requests}")
        print(f"Total requests: {self.total_requests}")
        print(f"Device ID: {self.device_id}")
        print(f"{'='*60}\n")

        connector = aiohttp.TCPConnector(limit=self.concurrent_requests, force_close=True)
        
        async with aiohttp.ClientSession(connector=connector) as session:
            start_time = time.perf_counter()
            
            # Send requests in batches
            remaining = self.total_requests
            request_id = 0
            
            while remaining > 0:
                batch_size = min(self.concurrent_requests, remaining)
                print(f"Sending batch: {batch_size} requests (remaining: {remaining})...")
                
                batch_results = await self.run_batch(session, request_id, batch_size)
                self.results.extend(batch_results)
                
                remaining -= batch_size
                request_id += batch_size
                
                # Progress update
                success_count = sum(1 for r in batch_results if r.success)
                print(f"  Batch complete: {success_count}/{batch_size} successful")

            total_time = time.perf_counter() - start_time

        self.print_results(total_time)

    def print_results(self, total_time: float):
        """Print test results summary."""
        successful = [r for r in self.results if r.success]
        failed = [r for r in self.results if not r.success]
        
        response_times = [r.response_time for r in self.results]
        avg_response_time = sum(response_times) / len(response_times) if response_times else 0
        min_response_time = min(response_times) if response_times else 0
        max_response_time = max(response_times) if response_times else 0
        
        # Calculate percentiles
        sorted_times = sorted(response_times)
        p50 = sorted_times[int(len(sorted_times) * 0.50)] if sorted_times else 0
        p90 = sorted_times[int(len(sorted_times) * 0.90)] if sorted_times else 0
        p99 = sorted_times[int(len(sorted_times) * 0.99)] if sorted_times else 0

        print(f"\n{'='*60}")
        print("RESULTS SUMMARY")
        print(f"{'='*60}")
        print(f"Total Requests:     {len(self.results)}")
        print(f"Successful:         {len(successful)} ({len(successful)/len(self.results)*100:.1f}%)")
        print(f"Failed:             {len(failed)} ({len(failed)/len(self.results)*100:.1f}%)")
        print(f"Total Time:         {total_time:.2f}s")
        print(f"Requests/sec:       {len(self.results)/total_time:.2f}")
        print(f"\nResponse Times:")
        print(f"  Min:              {min_response_time*1000:.2f}ms")
        print(f"  Max:              {max_response_time*1000:.2f}ms")
        print(f"  Average:          {avg_response_time*1000:.2f}ms")
        print(f"  P50:              {p50*1000:.2f}ms")
        print(f"  P90:              {p90*1000:.2f}ms")
        print(f"  P99:              {p99*1000:.2f}ms")
        
        # Error breakdown
        if failed:
            print(f"\nError Breakdown:")
            error_counts = {}
            for r in failed:
                key = r.error if r.error else f"HTTP {r.status_code}"
                error_counts[key] = error_counts.get(key, 0) + 1
            for error, count in sorted(error_counts.items(), key=lambda x: -x[1]):
                print(f"  {error}: {count}")
        
        print(f"{'='*60}\n")


async def main():
    parser = argparse.ArgumentParser(description="API Stress Test Tool")
    parser.add_argument(
        "--token",
        type=str,
        required=True,
        help="Bearer token for authentication",
    )
    parser.add_argument(
        "--device-id",
        type=str,
        required=True,
        help="Device ID to use in requests",
    )
    parser.add_argument(
        "--concurrent",
        type=int,
        default=500,
        help="Number of concurrent requests (default: 500)",
    )
    parser.add_argument(
        "--total",
        type=int,
        default=500,
        help="Total number of requests to send (default: 500)",
    )
    parser.add_argument(
        "--url",
        type=str,
        default="https://api.copilotai.click/api/store-dc-data",
        help="API URL to test",
    )

    args = parser.parse_args()

    stress_test = StressTest(
        url=args.url,
        bearer_token=args.token,
        device_id=args.device_id,
        concurrent_requests=args.concurrent,
        total_requests=args.total,
    )

    await stress_test.run()


if __name__ == "__main__":
    asyncio.run(main())
