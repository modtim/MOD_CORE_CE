import { aiAssistant } from './api.js';

// AI Chat State
let currentConversationId = null;
let currentWorkflowId = null;
let currentRunId = null;
let pollInterval = null;
let aiEnabled = false;

/**
 * Initialize AI chat for a failed workflow
 */
export async function initAIChat(nodeKind, dagRunId, workflowStatus) {
    currentWorkflowId = nodeKind;
    currentRunId = dagRunId;

    // Check if AI is enabled
    try {
        const status = await aiAssistant.getStatus();
        aiEnabled = status.enabled;

        if (!aiEnabled) {
            console.log('AI Assistant not enabled');
            return;
        }

        // Show AI chat panel for all workflows (not just failed ones)
        const chatPanel = document.getElementById('ai-chat-panel');
        if (chatPanel) {
            chatPanel.classList.remove('hidden');
        }
        // Claim the shared #ai-chat-form so the logs-aware LLM panel
        // (logs-llm-panel.js) stands down — both wire this same form.
        const chatForm = document.getElementById('ai-chat-form');
        if (chatForm) chatForm.dataset.mode = 'ai-chat';

        // Show status message
        showAIStatus(status);

    } catch (error) {
        console.error('Failed to check AI status:', error);
    }
}

/**
 * Show AI status banner
 */
function showAIStatus(status) {
    const statusDiv = document.getElementById('ai-chat-status');
    if (!statusDiv) return;

    if (!status.enabled) {
        statusDiv.className = 'mb-3 p-2 rounded text-sm bg-gray-200 text-gray-700';
        statusDiv.innerHTML = '⚠️ AI Assistant is not enabled';
        statusDiv.classList.remove('hidden');
        return;
    }

    if (status.available_workers === 0) {
        statusDiv.className = 'mb-3 p-2 rounded text-sm bg-yellow-100 text-yellow-800';
        statusDiv.innerHTML = `⚠️ No AI workers available. ${escapeHtml(status.message)}`;
        statusDiv.classList.remove('hidden');
        return;
    }

    // AI available
    statusDiv.className = 'mb-3 p-2 rounded text-sm bg-green-100 text-green-800';
    statusDiv.innerHTML = `✓ AI ready (${status.available_workers} worker(s), ~${status.estimated_wait_time})`;
    statusDiv.classList.remove('hidden');
}

/**
 * Start a new AI conversation
 */
export async function startConversation() {
    const input = document.getElementById('ai-chat-input');
    const sendBtn = document.getElementById('ai-chat-send');
    const startBtn = document.getElementById('start-ai-conversation');

    if (!input || !sendBtn) return;

    const initialMessage = input.value.trim();
    if (!initialMessage) {
        input.value = "What caused this workflow to fail?";
        return;
    }

    try {
        // Disable input
        input.disabled = true;
        sendBtn.disabled = true;
        if (startBtn) startBtn.disabled = true;

        // Show "AI is thinking" message
        addMessageToUI('user', initialMessage);
        addThinkingIndicator();

        // Start conversation
        const response = await aiAssistant.startConversation({
            workflow_id: currentWorkflowId,
            run_id: currentRunId,
            initial_message: initialMessage
        });

        currentConversationId = response.conversation_id;

        // Clear input
        input.value = '';

        // Start polling for response
        startPolling();

    } catch (error) {
        console.error('Failed to start conversation:', error);
        removeThinkingIndicator();
        addMessageToUI('error', `Failed to start conversation: ${error.message}`);
    } finally {
        input.disabled = false;
        sendBtn.disabled = false;
        if (startBtn) startBtn.disabled = false;
    }
}

/**
 * Send a message in existing conversation
 */
export async function sendMessage(message) {
    // If no conversation exists, start one instead
    if (!currentConversationId) {
        return startConversation();
    }

    const input = document.getElementById('ai-chat-input');
    const sendBtn = document.getElementById('ai-chat-send');

    if (!message) {
        message = input?.value?.trim();
        if (!message) return;
    }

    try {
        // Disable input
        if (input) input.disabled = true;
        if (sendBtn) sendBtn.disabled = true;

        // Show message
        addMessageToUI('user', message);
        addThinkingIndicator();

        // Send message
        await aiAssistant.sendMessage(currentConversationId, { message });

        // Clear input
        if (input) input.value = '';

        // Start polling
        startPolling();

    } catch (error) {
        console.error('Failed to send message:', error);
        removeThinkingIndicator();
        addMessageToUI('error', `Failed to send message: ${error.message}`);
    } finally {
        if (input) input.disabled = false;
        if (sendBtn) sendBtn.disabled = false;
    }
}

/**
 * Poll for conversation updates
 */
function startPolling() {
    if (pollInterval) {
        clearInterval(pollInterval);
    }

    pollInterval = setInterval(async () => {
        try {
            const conversation = await aiAssistant.getConversation(currentConversationId);

            // Check if we have a new AI response
            const messages = conversation.messages;
            const lastMessage = messages[messages.length - 1];

            if (lastMessage && lastMessage.role === 'assistant') {
                // Stop polling
                clearInterval(pollInterval);
                pollInterval = null;

                // Remove thinking indicator
                removeThinkingIndicator();

                // Add AI response
                addMessageToUI('assistant', lastMessage.content);
            }

        } catch (error) {
            console.error('Failed to poll conversation:', error);
            clearInterval(pollInterval);
            pollInterval = null;
            removeThinkingIndicator();
        }
    }, 2000); // Poll every 2 seconds
}

/**
 * Add message to UI
 */
function addMessageToUI(role, content) {
    const messagesContainer = document.getElementById('ai-chat-messages');
    if (!messagesContainer) return;

    // Remove welcome message if present
    const welcome = messagesContainer.querySelector('.text-center');
    if (welcome) {
        welcome.remove();
    }

    const messageDiv = document.createElement('div');
    messageDiv.className = `flex ${role === 'user' ? 'justify-end' : 'justify-start'}`;

    if (role === 'error') {
        messageDiv.innerHTML = `
            <div class="bg-red-100 text-red-800 rounded-lg px-3 py-2 max-w-sm text-sm">
                <strong>Error:</strong> ${content}
            </div>
        `;
    } else if (role === 'user') {
        messageDiv.innerHTML = `
            <div class="bg-primary text-white rounded-lg px-3 py-2 max-w-sm text-sm">
                ${escapeHtml(content)}
            </div>
        `;
    } else if (role === 'assistant') {
        // Render markdown for AI responses
        messageDiv.innerHTML = `
            <div class="bg-white border border-gray-200 rounded-lg px-3 py-2 max-w-lg text-sm">
                ${renderMarkdown(content)}
            </div>
        `;
    }

    messagesContainer.appendChild(messageDiv);
    messagesContainer.scrollTop = messagesContainer.scrollHeight;
}

/**
 * Add thinking indicator
 */
function addThinkingIndicator() {
    const messagesContainer = document.getElementById('ai-chat-messages');
    if (!messagesContainer) return;

    const thinkingDiv = document.createElement('div');
    thinkingDiv.id = 'ai-thinking-indicator';
    thinkingDiv.className = 'flex justify-start';
    thinkingDiv.innerHTML = `
        <div class="bg-gray-200 rounded-lg px-3 py-2 text-sm">
            <div class="flex gap-1">
                <div class="w-2 h-2 bg-gray-500 rounded-full animate-bounce" style="animation-delay: 0ms"></div>
                <div class="w-2 h-2 bg-gray-500 rounded-full animate-bounce" style="animation-delay: 150ms"></div>
                <div class="w-2 h-2 bg-gray-500 rounded-full animate-bounce" style="animation-delay: 300ms"></div>
            </div>
        </div>
    `;

    messagesContainer.appendChild(thinkingDiv);
    messagesContainer.scrollTop = messagesContainer.scrollHeight;
}

/**
 * Remove thinking indicator
 */
function removeThinkingIndicator() {
    const indicator = document.getElementById('ai-thinking-indicator');
    if (indicator) {
        indicator.remove();
    }
}

/**
 * Simple markdown renderer (basic formatting)
 */
function renderMarkdown(text) {
    // GSEC R-073: escape FIRST so model output can never inject markup; the
    // rules below only add tags around already-escaped text.
    text = escapeHtml(String(text == null ? '' : text));

    // Code blocks
    text = text.replace(/```([^`]+)```/g, '<pre class="bg-gray-900 text-green-400 p-2 rounded my-2 overflow-x-auto"><code>$1</code></pre>');

    // Inline code
    text = text.replace(/`([^`]+)`/g, '<code class="bg-gray-100 px-1 rounded">$1</code>');

    // Bold
    text = text.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');

    // Bullet lists
    text = text.replace(/^- (.+)$/gm, '<li class="ml-4">$1</li>');
    text = text.replace(/(<li.*<\/li>)/s, '<ul class="list-disc my-2">$1</ul>');

    // Line breaks
    text = text.replace(/\n/g, '<br>');

    return text;
}

/**
 * Escape HTML
 */
function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML.replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

/**
 * Reset AI chat when modal closes
 */
export function resetAIChat() {
    if (pollInterval) {
        clearInterval(pollInterval);
        pollInterval = null;
    }

    currentConversationId = null;
    currentWorkflowId = null;
    currentRunId = null;

    // Hide chat panel
    const chatPanel = document.getElementById('ai-chat-panel');
    if (chatPanel) {
        chatPanel.classList.add('hidden');
    }

    // Clear messages
    const messagesContainer = document.getElementById('ai-chat-messages');
    if (messagesContainer) {
        messagesContainer.innerHTML = `
            <div class="text-center text-gray-500 text-sm py-4">
                <svg class="w-10 h-10 mx-auto mb-2 text-gray-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z"></path>
                </svg>
                <p class="text-xs">Ask me about this workflow!</p>
                <p class="text-xs text-gray-400 mt-1">E.g., "What did this workflow do?"</p>
            </div>
        `;
    }

    // Clear input
    const input = document.getElementById('ai-chat-input');
    if (input) {
        input.value = '';
    }
}

/**
 * Setup event listeners
 */
let listenersSetup = false;

export function setupAIChatListeners() {
    // Prevent duplicate listeners
    if (listenersSetup) return;
    listenersSetup = true;

    // Start conversation button
    const startBtn = document.getElementById('start-ai-conversation');
    if (startBtn) {
        startBtn.addEventListener('click', startConversation);
    }

    // Chat form
    const form = document.getElementById('ai-chat-form');
    if (form) {
        form.addEventListener('submit', (e) => {
            e.preventDefault();
            // Shared form: when the logs-aware LLM panel owns the modal
            // (e.g. System Tasks / run-detail logs), let its handler
            // respond instead of the legacy workflow-AI chat.
            if (form.dataset.mode === 'logs-llm') return;
            e.stopPropagation();

            // Debounce - prevent double submission
            if (form.dataset.submitting === 'true') return;
            form.dataset.submitting = 'true';

            sendMessage().finally(() => {
                form.dataset.submitting = 'false';
            });
        });
    }
}
