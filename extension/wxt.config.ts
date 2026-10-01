import { defineConfig } from 'wxt';

export default defineConfig({
  modules: ['@wxt-dev/module-react'],
  manifest: {
    name: 'Compass',
    description: 'Ask any website where something is, and Compass takes you there.',
    action: {
      default_title: 'Open Compass', // tooltip on the toolbar icon
    },
    // Lets Compass see the URL of any tab and add the content script to it.
    host_permissions: ['<all_urls>'],
    permissions: [
      'storage', // the "Recent" list
      'scripting', // inject the content script into tabs opened before Compass was installed
    ],
    commands: {
      // Alt+C presses the toolbar button, which opens the side panel.
      _execute_action: {
        suggested_key: { default: 'Alt+C' },
      },
    },
  },
});
