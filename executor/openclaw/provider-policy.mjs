// Own replay policy uses the installed transport, without extra tools or network code.
import fs from 'node:fs';

export default {
  id: 'aist-novita-transport', name: 'AIST Novita transport policy',
  register(api) {
    for (const [id, label] of [['novita-aist', 'Novita through AIST'], ['anthropic-aist', 'Anthropic through AIST'], ['openai-aist', 'OpenAI through AIST']]) api.registerProvider({
      id, label, auth: [],
      wrapStreamFn({agentId, streamFn}) {
        const mapping = process.env.AIST_CONTEXT_MAP;
        if (!mapping) return streamFn;
        return (model, context, options = {}) => {
          const destination = new URL(model.baseUrl);
          if (destination.protocol !== 'http:' || destination.hostname !== '127.0.0.1') {
            throw Error('AIST context transport unavailable');
          }
          const route = JSON.parse(fs.readFileSync(mapping, 'utf8')).find(entry =>
            entry.agent_id === agentId && entry.session_id === options.sessionId && entry.active);
          if (!route) throw Error('AIST context unavailable');
          return streamFn(model, context, {...options, headers: {...options.headers,
            'x-aist-agent-id': agentId, 'x-aist-session-id': options.sessionId,
            'x-aist-capability': route.capability}});
        };
      },
      buildReplayPolicy() {
        return {
          sanitizeToolCallIds: false,
          repairToolUseResultPairing: false,
          allowSyntheticToolResults: false,
          dropThinkingBlocks: false,
          dropReasoningFromHistory: false,
          applyAssistantFirstOrderingFix: false,
          validateGeminiTurns: false,
          validateAnthropicTurns: false
        };
      }
    });
  }
};
