// Basic TypeScript file to test linting
export interface TikTokConfig {
  apiKey: string;
  region: string;
}

export class TikTokClient {
  private config: TikTokConfig;

  constructor(config: TikTokConfig) {
    this.config = config;
  }

  async initialize(): Promise<void> {
    // TODO: Implement initialization logic
    console.log('Initializing TikTok client...');
  }
}
