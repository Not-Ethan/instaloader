import os
import requests
import random
import time
import logging

logger = logging.getLogger(__name__)

class ProxyManager:
    def __init__(self):
        self.proxies_url = os.getenv("PROXIES_URL")
        self.proxies = []
        self.proxy_stats = {} # {proxy_str: {'last_used': timestamp, 'requests_this_min': int}}
        self.user_agents = [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.1.1 Safari/605.1.15",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:89.0) Gecko/20100101 Firefox/89.0",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.114 Safari/537.36",
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.101 Safari/537.36"
        ]

    def fetch_proxies(self):
        if not self.proxies_url:
            logger.warning("PROXIES_URL not set, skipping proxy fetch")
            return

        try:
            logger.info(f"Fetching proxies from {self.proxies_url}")
            response = requests.get(self.proxies_url)
            response.raise_for_status()
            lines = response.text.strip().split('\n')
            self.proxies = []
            for line in lines:
                if line.strip():
                    parts = line.strip().split(':')
                    if len(parts) == 4:
                        ip, port, user, password = parts
                        # Format for requests/instaloader: http://user:pass@ip:port
                        proxy_str = f"http://{user}:{password}@{ip}:{port}"
                        self.proxies.append(proxy_str)
                        if proxy_str not in self.proxy_stats:
                            self.proxy_stats[proxy_str] = {'last_used': 0, 'requests_this_min': 0}
            logger.info(f"Loaded {len(self.proxies)} proxies")
        except Exception as e:
            logger.error(f"Failed to fetch proxies: {e}")

    def get_proxy(self):
        if not self.proxies:
            return None
        
        # Simple rotation with rate limit check
        now = time.time()
        
        # Shuffle to randomize selection
        random.shuffle(self.proxies)
        
        for proxy in self.proxies:
            stats = self.proxy_stats.get(proxy)
            if not stats:
                stats = {'last_used': 0, 'requests_this_min': 0}
                self.proxy_stats[proxy] = stats
            
            # Reset counter if minute has passed
            if now - stats['last_used'] > 60:
                stats['requests_this_min'] = 0
            
            if stats['requests_this_min'] < 10:
                stats['requests_this_min'] += 1
                stats['last_used'] = now
                return proxy
        
        # If all are rate limited, just return a random one
        logger.warning("All proxies rate limited, picking random one")
        return random.choice(self.proxies)

    def get_user_agent(self):
        return random.choice(self.user_agents)
