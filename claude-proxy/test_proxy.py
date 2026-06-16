"""测试脚本"""
import httpx
import asyncio
import json

__test__ = False


async def test_non_stream():
    """测试非流式请求"""
    print("=== 测试非流式请求 ===")
    
    async with httpx.AsyncClient() as client:
        response = await client.post(
            "http://127.0.0.1:8000/v1/messages",
            json={
                "model": "claude-3-5-sonnet-20241022",
                "max_tokens": 1024,
                "messages": [
                    {"role": "user", "content": "你好,请用一句话介绍你自己"}
                ]
            },
            headers={"x-api-key": "test-key"},
            timeout=30.0
        )
        
        print(f"状态码: {response.status_code}")
        result = response.json()
        print(f"响应: {json.dumps(result, indent=2, ensure_ascii=False)}")


async def test_stream():
    """测试流式请求"""
    print("\n=== 测试流式请求 ===")
    
    async with httpx.AsyncClient() as client:
        async with client.stream(
            "POST",
            "http://127.0.0.1:8000/v1/messages",
            json={
                "model": "claude-3-5-sonnet-20241022",
                "max_tokens": 1024,
                "messages": [
                    {"role": "user", "content": "数到 5"}
                ],
                "stream": True
            },
            headers={"x-api-key": "test-key"},
            timeout=30.0
        ) as response:
            print(f"状态码: {response.status_code}")
            print("流式输出:")
            async for line in response.aiter_lines():
                if line.strip():
                    print(line)


async def test_health():
    """测试健康检查"""
    print("\n=== 测试健康检查 ===")
    
    async with httpx.AsyncClient() as client:
        response = await client.get("http://127.0.0.1:8000/health")
        print(f"状态码: {response.status_code}")
        print(f"响应: {json.dumps(response.json(), indent=2, ensure_ascii=False)}")


async def main():
    """主函数"""
    try:
        await test_health()
        await test_non_stream()
        await test_stream()
    except Exception as e:
        print(f"错误: {e}")


if __name__ == "__main__":
    asyncio.run(main())
