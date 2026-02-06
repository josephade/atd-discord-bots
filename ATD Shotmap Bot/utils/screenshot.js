// const puppeteer = require('puppeteer');
// const fs = require('fs-extra');
// const path = require('path');

// // Browser instance
// let browser = null;

// async function getBrowser() {
//   if (!browser) {
//     console.log('🌐 Launching browser...');
//     browser = await puppeteer.launch({
//       headless: 'new',
//       args: [
//         '--no-sandbox',
//         '--disable-setuid-sandbox',
//         '--disable-dev-shm-usage',
//         '--disable-web-security',
//         '--disable-features=IsolateOrigins,site-per-process'
//       ],
//       defaultViewport: {
//         width: 1400,
//         height: 1000
//       }
//     });
    
//     process.on('SIGINT', async () => {
//       if (browser) {
//         await browser.close();
//       }
//       process.exit(0);
//     });
//   }
//   return browser;
// }

// async function generateShotmap(playerName, year, seasonType = 'RS') {
//   console.log(`🎯 Generating shotmap for: ${playerName} - ${year}`);
  
//   const browser = await getBrowser();
//   const page = await browser.newPage();
  
//   try {
//     // Set timeouts
//     page.setDefaultTimeout(60000);
//     page.setDefaultNavigationTimeout(60000);
    
//     // Set user agent
//     await page.setUserAgent('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36');
    
//     // Navigate to NBA Visuals
//     console.log('🔗 Navigating to nbavisuals.com/shotmap...');
//     await page.goto('https://nbavisuals.com/shotmap', {
//       waitUntil: 'networkidle0',
//       timeout: 60000
//     });
    
//     console.log('✅ Page loaded successfully');
    
//     // Wait for the form to load
//     await page.waitForSelector('#season-dropdown', { timeout: 10000 });
//     await page.waitForSelector('#playerSearch', { timeout: 10000 });
    
//     // Save initial page for debugging
//     await page.screenshot({ path: 'nba_initial.png' });
//     console.log('📸 Saved initial page screenshot');
    
//     // Step 1: Select season(s)
//     console.log(`📅 Selecting season: ${year}`);
    
//     // Convert year format (e.g., "2024" to "2024-25")
//     const seasonFormat = `${year}-${(parseInt(year) + 1).toString().slice(-2)}`;
//     console.log(`   Using season format: ${seasonFormat}`);
    
//     // Clear any existing season selections
//     console.log('   Clearing existing season selections...');
//     await page.evaluate(() => {
//       const seasonSelect = document.getElementById('season-dropdown');
//       for (let i = 0; i < seasonSelect.options.length; i++) {
//         seasonSelect.options[i].selected = false;
//       }
//       seasonSelect.dispatchEvent(new Event('change', { bubbles: true }));
//     });
    
//     // Select the specific season
//     await page.select('#season-dropdown', seasonFormat);
//     await page.waitForTimeout(1000);
//     console.log(`✅ Season ${seasonFormat} selected`);
    
//     // Step 2: Search for and select player (MOUSE-FIRSTR approach)
//     console.log(`👤 Selecting player: ${playerName}`);
//     console.log('   Using MOUSE-FIRST selection strategy...');
    
//     // Clear any existing player selections
//     console.log('   Clearing existing player selections...');
//     await page.evaluate(() => {
//       // Clear search input
//       const searchInput = document.getElementById('playerSearch');
//       if (searchInput) {
//         searchInput.value = '';
//         searchInput.dispatchEvent(new Event('input', { bubbles: true }));
//       }
      
//       // Remove any selected pills/chips
//       const removeButtons = document.querySelectorAll('.choices__button');
//       removeButtons.forEach(btn => {
//         if (btn.style.display !== 'none') {
//           btn.click();
//         }
//       });
//     });
    
//     await page.waitForTimeout(1000);
    
//     // Click the player search input
//     console.log('   Clicking player search input...');
//     await page.click('#playerSearch');
//     await page.waitForTimeout(500);
    
//     // Type the player name to trigger dropdown
//     console.log(`   Typing player name: "${playerName}"...`);
//     await page.type('#playerSearch', playerName);
//     await page.waitForTimeout(2000); // Wait for dropdown to populate
    
//     // DEBUG: Log dropdown state before selection
//     console.log('   Checking dropdown state...');
//     const dropdownState = await page.evaluate(() => {
//       const dropdown = document.querySelector('.choices__list--dropdown');
//       const options = dropdown ? dropdown.querySelectorAll('.choices__item--choice') : [];
//       return {
//         hasDropdown: !!dropdown,
//         dropdownVisible: dropdown ? dropdown.style.display !== 'none' : false,
//         optionCount: options.length,
//         optionTexts: Array.from(options).map(opt => opt.textContent?.trim()).slice(0, 5)
//       };
//     });
    
//     console.log(`   Dropdown state:`, dropdownState);
    
//     // STRATEGY 1: Try to click the dropdown option with mouse
//     console.log('   STRATEGY 1: Attempting mouse click selection...');
//     const mouseSelectionSuccess = await page.evaluate((targetPlayer) => {
//       // Look for dropdown options
//       const dropdown = document.querySelector('.choices__list--dropdown');
//       if (!dropdown) {
//         console.log('   No dropdown found');
//         return false;
//       }
      
//       const options = dropdown.querySelectorAll('.choices__item--choice');
//       console.log(`   Found ${options.length} dropdown options`);
      
//       // Try exact match first
//       for (const option of options) {
//         const text = option.textContent || '';
//         const cleanText = text.trim();
//         console.log(`   Checking option: "${cleanText}"`);
        
//         if (cleanText.toLowerCase() === targetPlayer.toLowerCase()) {
//           console.log(`   Found exact match, clicking...`);
//           option.click();
//           return true;
//         }
//       }
      
//       // Try partial match
//       for (const option of options) {
//         const text = option.textContent || '';
//         const cleanText = text.trim();
        
//         const targetFirstName = targetPlayer.split(' ')[0].toLowerCase();
//         const targetLastName = targetPlayer.split(' ')[1]?.toLowerCase();
        
//         if (cleanText.toLowerCase().includes(targetFirstName) || 
//             (targetLastName && cleanText.toLowerCase().includes(targetLastName))) {
//           console.log(`   Found partial match, clicking...`);
//           option.click();
//           return true;
//         }
//       }
      
//       // Click first option as fallback
//       if (options.length > 0) {
//         console.log(`   No match found, clicking first option...`);
//         options[0].click();
//         return true;
//       }
      
//       return false;
//     }, playerName);
    
//     if (mouseSelectionSuccess) {
//       console.log('✅ Mouse selection successful');
//     } else {
//       console.log('❌ Mouse selection failed, trying STRATEGY 2...');
      
//       // STRATEGY 2: Use keyboard navigation
//       console.log('   STRATEGY 2: Using keyboard navigation (ArrowDown + Enter)...');
      
//       // Clear and retry with keyboard
//       await page.evaluate(() => {
//         const searchInput = document.getElementById('playerSearch');
//         if (searchInput) {
//           searchInput.value = '';
//           searchInput.dispatchEvent(new Event('input', { bubbles: true }));
//         }
//       });
      
//       await page.waitForTimeout(1000);
      
//       // Type again
//       await page.type('#playerSearch', playerName);
//       await page.waitForTimeout(2000);
      
//       // Press ArrowDown multiple times to ensure selection
//       console.log('   Pressing ArrowDown (3 times)...');
//       for (let i = 0; i < 3; i++) {
//         await page.keyboard.press('ArrowDown');
//         await page.waitForTimeout(300);
//       }
      
//       // Press Enter to select
//       console.log('   Pressing Enter to confirm selection...');
//       await page.keyboard.press('Enter');
//     }
    
//     await page.waitForTimeout(2000);
    
//     // Verify player selection
//     console.log('   Verifying player selection...');
//     const verification = await page.evaluate((expectedPlayer) => {
//       const searchInput = document.getElementById('playerSearch');
//       const inputValue = searchInput ? searchInput.value : '';
      
//       // Check selected items in the UI
//       const selectedItems = document.querySelectorAll('.choices__item--selectable');
//       const selectedTexts = Array.from(selectedItems).map(item => item.textContent?.trim());
      
//       // Check hidden select element
//       const hiddenSelect = document.getElementById('players-dropdown');
//       const hiddenSelected = [];
//       if (hiddenSelect) {
//         for (let i = 0; i < hiddenSelect.options.length; i++) {
//           if (hiddenSelect.options[i].selected) {
//             hiddenSelected.push(hiddenSelect.options[i].text);
//           }
//         }
//       }
      
//       return {
//         inputValue,
//         selectedInUI: selectedTexts,
//         selectedInHidden: hiddenSelected,
//         matchesExpected: inputValue.toLowerCase().includes(expectedPlayer.toLowerCase()) || 
//                          selectedTexts.some(text => text.toLowerCase().includes(expectedPlayer.toLowerCase()))
//       };
//     }, playerName);
    
//     console.log('   Selection verification:', verification);
    
//     if (!verification.matchesExpected) {
//       console.log(`⚠️ WARNING: Player "${playerName}" may not be selected correctly`);
//       console.log(`   Search input shows: "${verification.inputValue}"`);
//       console.log(`   UI selections: ${verification.selectedInUI.join(', ')}`);
//       console.log(`   Hidden selections: ${verification.selectedInHidden.join(', ')}`);
//     } else {
//       console.log(`✅ Player "${playerName}" selected successfully`);
//     }
    
//     // Save screenshot after player selection
//     await page.screenshot({ path: 'nba_player_selected.png' });
//     console.log('📸 Saved player selection screenshot');
    
//     // Step 3: Click "Generate Shotmap" button
//     console.log('🔄 Clicking Generate Shotmap button...');
    
//     await page.evaluate(() => {
//       const generateButton = document.getElementById('generateGraphButton');
//       if (generateButton) {
//         generateButton.click();
//         return true;
//       }
//       return false;
//     });
    
//     console.log('✅ Generate button clicked');
    
//     // Wait for the form submission
//     await page.waitForTimeout(3000);
    
//     // Check if graph is loading
//     console.log('🔍 Checking if graph is loading...');
//     const graphLoading = await page.evaluate(() => {
//       const graphContainer = document.getElementById('graph-container');
//       if (graphContainer) {
//         const content = graphContainer.textContent || '';
//         const hasContent = content.trim().length > 0;
//         const isLoading = content.includes('Loading') || content.includes('Generating');
//         const hasGraph = graphContainer.querySelector('svg, canvas, img') || 
//                         graphContainer.innerHTML.includes('plotly');
        
//         return {
//           hasContainer: true,
//           contentPreview: content.substring(0, 100),
//           hasContent,
//           isLoading,
//           hasGraph
//         };
//       }
//       return { hasContainer: false };
//     });
    
//     console.log('   Graph loading state:', graphLoading);
    
//     // Step 4: Wait for graph to load
//     console.log('⏳ Waiting for shotmap to generate (15-20 seconds)...');
    
//     let graphLoaded = false;
//     let attempts = 0;
//     const maxAttempts = 10;
    
//     while (attempts < maxAttempts && !graphLoaded) {
//       attempts++;
//       await page.waitForTimeout(3000);
      
//       graphLoaded = await page.evaluate(() => {
//         const graphContainer = document.getElementById('graph-container');
//         if (!graphContainer) return false;
        
//         // Check for visual elements
//         const hasVisual = graphContainer.querySelector('svg, canvas, img, [class*="plot"]');
        
//         // Check for meaningful content
//         const text = graphContainer.textContent || '';
//         const hasMeaningfulText = text.length > 50 && !text.includes('No graph available');
        
//         // Check for specific shotmap elements
//         const hasShotmapElements = text.includes('eFG%') || 
//                                   text.includes('Shots:') || 
//                                   text.includes('Zone') ||
//                                   text.includes('FREQ') ||
//                                   text.includes('FG%');
        
//         return hasVisual || (hasMeaningfulText && hasShotmapElements);
//       });
      
//       if (!graphLoaded) {
//         console.log(`   Attempt ${attempts}/${maxAttempts}: Still waiting for graph...`);
//       }
//     }
    
//     if (graphLoaded) {
//       console.log('✅ Graph loaded successfully');
//     } else {
//       console.log('⚠️ Graph may not have loaded completely');
//     }
    
//     // Give extra time for rendering
//     console.log('🎨 Allowing final rendering (3 seconds)...');
//     await page.waitForTimeout(3000);
    
//     // Save intermediate screenshot
//     await page.screenshot({ path: 'nba_before_capture.png' });
//     console.log('📸 Saved pre-capture screenshot');
    
//     // Step 5: Find and capture the graph area
//     console.log('🔍 Finding graph container for screenshot...');
    
//     const graphContainerInfo = await page.evaluate(() => {
//       // First try the main graph container
//       const graphContainer = document.getElementById('graph-container');
//       if (graphContainer && graphContainer.children.length > 0) {
//         const rect = graphContainer.getBoundingClientRect();
//         console.log(`   Found graph-container: ${rect.width}x${rect.height}`);
//         return {
//           type: 'graph-container',
//           x: rect.x,
//           y: rect.y,
//           width: rect.width,
//           height: rect.height,
//           hasContent: true
//         };
//       }
      
//       // Look for the shotmap visualization
//       const shotmapVis = document.querySelector('[class*="shotmap"], [class*="visualization"]');
//       if (shotmapVis) {
//         const rect = shotmapVis.getBoundingClientRect();
//         console.log(`   Found shotmap visualization: ${rect.width}x${rect.height}`);
//         return {
//           type: 'shotmap-vis',
//           x: rect.x,
//           y: rect.y,
//           width: rect.width,
//           height: rect.height,
//           hasContent: true
//         };
//       }
      
//       // Look for plotly charts
//       const plotlyChart = document.querySelector('.plotly, .js-plotly-plot');
//       if (plotlyChart) {
//         const rect = plotlyChart.getBoundingClientRect();
//         console.log(`   Found plotly chart: ${rect.width}x${rect.height}`);
//         return {
//           type: 'plotly',
//           x: rect.x,
//           y: rect.y,
//           width: rect.width,
//           height: rect.height,
//           hasContent: true
//         };
//       }
      
//       // Fallback: main content area
//       const mainContent = document.querySelector('.container.mx-auto');
//       if (mainContent) {
//         const rect = mainContent.getBoundingClientRect();
//         console.log(`   Using main container: ${rect.width}x${rect.height}`);
//         return {
//           type: 'main-container',
//           x: rect.x,
//           y: rect.y,
//           width: rect.width,
//           height: rect.height,
//           hasContent: true
//         };
//       }
      
//       // Last resort: fixed area
//       console.log(`   Using fixed area fallback`);
//       return {
//         type: 'fallback',
//         x: 50,
//         y: 200,
//         width: 900,
//         height: 700,
//         hasContent: false
//       };
//     });
    
//     console.log(`📐 Selected ${graphContainerInfo.type}: ${graphContainerInfo.width}x${graphContainerInfo.height} at (${graphContainerInfo.x}, ${graphContainerInfo.y})`);
    
//     // Step 6: Take screenshot
//     console.log('📸 Taking final screenshot...');
    
//     const screenshot = await page.screenshot({
//       type: 'png',
//       clip: {
//         x: Math.max(0, graphContainerInfo.x - 10),
//         y: Math.max(0, graphContainerInfo.y - 10),
//         width: Math.min(graphContainerInfo.width + 20, 1200),
//         height: Math.min(graphContainerInfo.height + 20, 800)
//       },
//       encoding: 'binary'
//     });
    
//     // Save final screenshot
//     fs.writeFileSync('nba_final_result.png', screenshot);
//     console.log('✅ Shotmap generated and saved successfully');
    
//     return screenshot;
    
//   } catch (error) {
//     console.error('❌ Error generating shotmap:', error.message);
//     console.error('Stack trace:', error.stack);
    
//     // Save error screenshot
//     try {
//       const errorScreenshot = await page.screenshot({ 
//         path: 'nba_error.png',
//         fullPage: true,
//         encoding: 'binary'
//       });
//       console.log('📁 Saved error screenshot to nba_error.png');
//     } catch (e) {
//       console.error('Could not save error screenshot:', e.message);
//     }
    
//     throw error;
//   } finally {
//     await page.close();
//     console.log('🔄 Browser page closed');
//   }
// }

// module.exports = {
//   generateShotmap: generateShotmap
// };





















// const puppeteer = require('puppeteer');
// const fs = require('fs-extra');
// const path = require('path');

// // Browser instance
// let browser = null;

// async function getBrowser() {
//   if (!browser) {
//     console.log('🌐 Launching browser...');
//     browser = await puppeteer.launch({
//       headless: 'new',
//       args: [
//         '--no-sandbox',
//         '--disable-setuid-sandbox',
//         '--disable-dev-shm-usage',
//         '--disable-web-security',
//         '--disable-features=IsolateOrigins,site-per-process'
//       ],
//       defaultViewport: {
//         width: 1400,
//         height: 1000
//       }
//     });
    
//     process.on('SIGINT', async () => {
//       if (browser) {
//         await browser.close();
//       }
//       process.exit(0);
//     });
//   }
//   return browser;
// }

// async function generateShotmap(playerName, year, seasonType = 'RS') {
//   console.log(`🎯 Generating shotmap for: ${playerName} - ${year}`);
  
//   const browser = await getBrowser();
//   const page = await browser.newPage();
  
//   try {
//     // Set timeouts
//     page.setDefaultTimeout(60000);
//     page.setDefaultNavigationTimeout(60000);
    
//     // Set user agent
//     await page.setUserAgent('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36');
    
//     // Navigate to NBA Visuals
//     console.log('🔗 Navigating to nbavisuals.com/shotmap...');
//     await page.goto('https://nbavisuals.com/shotmap', {
//       waitUntil: 'networkidle0',
//       timeout: 60000
//     });
    
//     console.log('✅ Page loaded successfully');
    
//     // Wait for the form to load
//     await page.waitForSelector('#season-dropdown', { timeout: 10000 });
//     await page.waitForSelector('#playerSearch', { timeout: 10000 });
    
//     // Step 1: Select season(s)
//     console.log(`📅 Selecting season: ${year}`);
    
//     // Convert year format (e.g., "2024" to "2024-25")
//     const seasonFormat = `${year}-${(parseInt(year) + 1).toString().slice(-2)}`;
//     console.log(`   Using season format: ${seasonFormat}`);
    
//     // Clear any existing season selections
//     console.log('   Clearing existing season selections...');
//     await page.evaluate(() => {
//       const seasonSelect = document.getElementById('season-dropdown');
//       for (let i = 0; i < seasonSelect.options.length; i++) {
//         seasonSelect.options[i].selected = false;
//       }
//       seasonSelect.dispatchEvent(new Event('change', { bubbles: true }));
//     });
    
//     // Select the specific season
//     await page.select('#season-dropdown', seasonFormat);
//     await page.waitForTimeout(1000);
//     console.log(`✅ Season ${seasonFormat} selected`);
    
//     // Step 2: Search for and select player (MOUSE-FIRSTR approach)
//     console.log(`👤 Selecting player: ${playerName}`);
//     console.log('   Using MOUSE-FIRST selection strategy...');
    
//     // Clear any existing player selections
//     console.log('   Clearing existing player selections...');
//     await page.evaluate(() => {
//       // Clear search input
//       const searchInput = document.getElementById('playerSearch');
//       if (searchInput) {
//         searchInput.value = '';
//         searchInput.dispatchEvent(new Event('input', { bubbles: true }));
//       }
      
//       // Remove any selected pills/chips
//       const removeButtons = document.querySelectorAll('.choices__button');
//       removeButtons.forEach(btn => {
//         if (btn.style.display !== 'none') {
//           btn.click();
//         }
//       });
//     });
    
//     await page.waitForTimeout(1000);
    
//     // Click the player search input
//     console.log('   Clicking player search input...');
//     await page.click('#playerSearch');
//     await page.waitForTimeout(500);
    
//     // Type the player name to trigger dropdown
//     console.log(`   Typing player name: "${playerName}"...`);
//     await page.type('#playerSearch', playerName);
//     await page.waitForTimeout(2000); // Wait for dropdown to populate
    
//     // DEBUG: Log dropdown state before selection
//     console.log('   Checking dropdown state...');
//     const dropdownState = await page.evaluate(() => {
//       const dropdown = document.querySelector('.choices__list--dropdown');
//       const options = dropdown ? dropdown.querySelectorAll('.choices__item--choice') : [];
//       return {
//         hasDropdown: !!dropdown,
//         dropdownVisible: dropdown ? dropdown.style.display !== 'none' : false,
//         optionCount: options.length,
//         optionTexts: Array.from(options).map(opt => opt.textContent?.trim()).slice(0, 5)
//       };
//     });
    
//     console.log(`   Dropdown state:`, dropdownState);
    
//     // STRATEGY 1: Try to click the dropdown option with mouse
//     console.log('   STRATEGY 1: Attempting mouse click selection...');
//     const mouseSelectionSuccess = await page.evaluate((targetPlayer) => {
//       // Look for dropdown options
//       const dropdown = document.querySelector('.choices__list--dropdown');
//       if (!dropdown) {
//         console.log('   No dropdown found');
//         return false;
//       }
      
//       const options = dropdown.querySelectorAll('.choices__item--choice');
//       console.log(`   Found ${options.length} dropdown options`);
      
//       // Try exact match first
//       for (const option of options) {
//         const text = option.textContent || '';
//         const cleanText = text.trim();
//         console.log(`   Checking option: "${cleanText}"`);
        
//         if (cleanText.toLowerCase() === targetPlayer.toLowerCase()) {
//           console.log(`   Found exact match, clicking...`);
//           option.click();
//           return true;
//         }
//       }
      
//       // Try partial match
//       for (const option of options) {
//         const text = option.textContent || '';
//         const cleanText = text.trim();
        
//         const targetFirstName = targetPlayer.split(' ')[0].toLowerCase();
//         const targetLastName = targetPlayer.split(' ')[1]?.toLowerCase();
        
//         if (cleanText.toLowerCase().includes(targetFirstName) || 
//             (targetLastName && cleanText.toLowerCase().includes(targetLastName))) {
//           console.log(`   Found partial match, clicking...`);
//           option.click();
//           return true;
//         }
//       }
      
//       // Click first option as fallback
//       if (options.length > 0) {
//         console.log(`   No match found, clicking first option...`);
//         options[0].click();
//         return true;
//       }
      
//       return false;
//     }, playerName);
    
//     if (mouseSelectionSuccess) {
//       console.log('✅ Mouse selection successful');
//     } else {
//       console.log('❌ Mouse selection failed, trying STRATEGY 2...');
      
//       // STRATEGY 2: Use keyboard navigation
//       console.log('   STRATEGY 2: Using keyboard navigation (ArrowDown + Enter)...');
      
//       // Clear and retry with keyboard
//       await page.evaluate(() => {
//         const searchInput = document.getElementById('playerSearch');
//         if (searchInput) {
//           searchInput.value = '';
//           searchInput.dispatchEvent(new Event('input', { bubbles: true }));
//         }
//       });
      
//       await page.waitForTimeout(1000);
      
//       // Type again
//       await page.type('#playerSearch', playerName);
//       await page.waitForTimeout(2000);
      
//       // Press ArrowDown multiple times to ensure selection
//       console.log('   Pressing ArrowDown (3 times)...');
//       for (let i = 0; i < 3; i++) {
//         await page.keyboard.press('ArrowDown');
//         await page.waitForTimeout(300);
//       }
      
//       // Press Enter to select
//       console.log('   Pressing Enter to confirm selection...');
//       await page.keyboard.press('Enter');
//     }
    
//     await page.waitForTimeout(2000);
    
//     // Verify player selection
//     console.log('   Verifying player selection...');
//     const verification = await page.evaluate((expectedPlayer) => {
//       const searchInput = document.getElementById('playerSearch');
//       const inputValue = searchInput ? searchInput.value : '';
      
//       // Check selected items in the UI
//       const selectedItems = document.querySelectorAll('.choices__item--selectable');
//       const selectedTexts = Array.from(selectedItems).map(item => item.textContent?.trim());
      
//       // Check hidden select element
//       const hiddenSelect = document.getElementById('players-dropdown');
//       const hiddenSelected = [];
//       if (hiddenSelect) {
//         for (let i = 0; i < hiddenSelect.options.length; i++) {
//           if (hiddenSelect.options[i].selected) {
//             hiddenSelected.push(hiddenSelect.options[i].text);
//           }
//         }
//       }
      
//       return {
//         inputValue,
//         selectedInUI: selectedTexts,
//         selectedInHidden: hiddenSelected,
//         matchesExpected: inputValue.toLowerCase().includes(expectedPlayer.toLowerCase()) || 
//                          selectedTexts.some(text => text.toLowerCase().includes(expectedPlayer.toLowerCase()))
//       };
//     }, playerName);
    
//     console.log('   Selection verification:', verification);
    
//     if (!verification.matchesExpected) {
//       console.log(`⚠️ WARNING: Player "${playerName}" may not be selected correctly`);
//       console.log(`   Search input shows: "${verification.inputValue}"`);
//       console.log(`   UI selections: ${verification.selectedInUI.join(', ')}`);
//       console.log(`   Hidden selections: ${verification.selectedInHidden.join(', ')}`);
//     } else {
//       console.log(`✅ Player "${playerName}" selected successfully`);
//     }
    
//     // Step 3: Click "Generate Shotmap" button
//     console.log('🔄 Clicking Generate Shotmap button...');
    
//     await page.evaluate(() => {
//       const generateButton = document.getElementById('generateGraphButton');
//       if (generateButton) {
//         generateButton.click();
//         return true;
//       }
//       return false;
//     });
    
//     console.log('✅ Generate button clicked');
    
//     // Wait for the form submission
//     await page.waitForTimeout(3000);
    
//     // Check if graph is loading
//     console.log('🔍 Checking if graph is loading...');
//     const graphLoading = await page.evaluate(() => {
//       const graphContainer = document.getElementById('graph-container');
//       if (graphContainer) {
//         const content = graphContainer.textContent || '';
//         const hasContent = content.trim().length > 0;
//         const isLoading = content.includes('Loading') || content.includes('Generating');
//         const hasGraph = graphContainer.querySelector('svg, canvas, img') || 
//                         graphContainer.innerHTML.includes('plotly');
        
//         return {
//           hasContainer: true,
//           contentPreview: content.substring(0, 100),
//           hasContent,
//           isLoading,
//           hasGraph
//         };
//       }
//       return { hasContainer: false };
//     });
    
//     console.log('   Graph loading state:', graphLoading);
    
//     // Step 4: Wait for graph to load
//     console.log('⏳ Waiting for shotmap to generate (15-20 seconds)...');
    
//     let graphLoaded = false;
//     let attempts = 0;
//     const maxAttempts = 10;
    
//     while (attempts < maxAttempts && !graphLoaded) {
//       attempts++;
//       await page.waitForTimeout(3000);
      
//       graphLoaded = await page.evaluate(() => {
//         const graphContainer = document.getElementById('graph-container');
//         if (!graphContainer) return false;
        
//         // Check for visual elements
//         const hasVisual = graphContainer.querySelector('svg, canvas, img, [class*="plot"]');
        
//         // Check for meaningful content
//         const text = graphContainer.textContent || '';
//         const hasMeaningfulText = text.length > 50 && !text.includes('No graph available');
        
//         // Check for specific shotmap elements
//         const hasShotmapElements = text.includes('eFG%') || 
//                                   text.includes('Shots:') || 
//                                   text.includes('Zone') ||
//                                   text.includes('FREQ') ||
//                                   text.includes('FG%');
        
//         return hasVisual || (hasMeaningfulText && hasShotmapElements);
//       });
      
//       if (!graphLoaded) {
//         console.log(`   Attempt ${attempts}/${maxAttempts}: Still waiting for graph...`);
//       }
//     }
    
//     if (graphLoaded) {
//       console.log('✅ Graph loaded successfully');
//     } else {
//       console.log('⚠️ Graph may not have loaded completely');
//     }
    
//     // Give extra time for rendering
//     console.log('🎨 Allowing final rendering (3 seconds)...');
//     await page.waitForTimeout(3000);
    
//     // Step 5: Find and capture the graph area
//     console.log('🔍 Finding graph container for screenshot...');
    
//     const graphContainerInfo = await page.evaluate(() => {
//       // First try the main graph container
//       const graphContainer = document.getElementById('graph-container');
//       if (graphContainer && graphContainer.children.length > 0) {
//         const rect = graphContainer.getBoundingClientRect();
//         console.log(`   Found graph-container: ${rect.width}x${rect.height}`);
//         return {
//           type: 'graph-container',
//           x: rect.x,
//           y: rect.y,
//           width: rect.width,
//           height: rect.height,
//           hasContent: true
//         };
//       }
      
//       // Look for the shotmap visualization
//       const shotmapVis = document.querySelector('[class*="shotmap"], [class*="visualization"]');
//       if (shotmapVis) {
//         const rect = shotmapVis.getBoundingClientRect();
//         console.log(`   Found shotmap visualization: ${rect.width}x${rect.height}`);
//         return {
//           type: 'shotmap-vis',
//           x: rect.x,
//           y: rect.y,
//           width: rect.width,
//           height: rect.height,
//           hasContent: true
//         };
//       }
      
//       // Look for plotly charts
//       const plotlyChart = document.querySelector('.plotly, .js-plotly-plot');
//       if (plotlyChart) {
//         const rect = plotlyChart.getBoundingClientRect();
//         console.log(`   Found plotly chart: ${rect.width}x${rect.height}`);
//         return {
//           type: 'plotly',
//           x: rect.x,
//           y: rect.y,
//           width: rect.width,
//           height: rect.height,
//           hasContent: true
//         };
//       }
      
//       // Fallback: main content area
//       const mainContent = document.querySelector('.container.mx-auto');
//       if (mainContent) {
//         const rect = mainContent.getBoundingClientRect();
//         console.log(`   Using main container: ${rect.width}x${rect.height}`);
//         return {
//           type: 'main-container',
//           x: rect.x,
//           y: rect.y,
//           width: rect.width,
//           height: rect.height,
//           hasContent: true
//         };
//       }
      
//       // Last resort: fixed area
//       console.log(`   Using fixed area fallback`);
//       return {
//         type: 'fallback',
//         x: 50,
//         y: 200,
//         width: 900,
//         height: 700,
//         hasContent: false
//       };
//     });
    
//     console.log(`📐 Selected ${graphContainerInfo.type}: ${graphContainerInfo.width}x${graphContainerInfo.height} at (${graphContainerInfo.x}, ${graphContainerInfo.y})`);
    
//     // Step 6: Take screenshot
//     console.log('📸 Taking final screenshot...');
    
//     const screenshot = await page.screenshot({
//       type: 'png',
//       clip: {
//         x: Math.max(0, graphContainerInfo.x - 10),
//         y: Math.max(0, graphContainerInfo.y - 10),
//         width: Math.min(graphContainerInfo.width + 20, 1200),
//         height: Math.min(graphContainerInfo.height + 20, 800)
//       },
//       encoding: 'binary'
//     });
    
//     console.log('✅ Shotmap generated successfully');
    
//     return screenshot;
    
//   } catch (error) {
//     console.error('❌ Error generating shotmap:', error.message);
//     console.error('Stack trace:', error.stack);
    
//     throw error;
//   } finally {
//     await page.close();
//     console.log('🔄 Browser page closed');
//   }
// }

// module.exports = {
//   generateShotmap: generateShotmap
// };























// const puppeteer = require('puppeteer');
// const fs = require('fs-extra');
// const path = require('path');

// let browser = null;

// async function getBrowser() {
//   if (!browser) {
//     browser = await puppeteer.launch({
//       headless: 'new',
//       args: ['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage'],
//       defaultViewport: { width: 1400, height: 1000 }
//     });
//   }
//   return browser;
// }

// async function generateShotmap(playerName, year, seasonType = 'RS') {
//   console.log(`🎯 Generating shotmap for: ${playerName} - ${year}`);
  
//   const browser = await getBrowser();
//   const page = await browser.newPage();
  
//   try {
//     page.setDefaultTimeout(30000);
//     page.setDefaultNavigationTimeout(30000);
    
//     await page.setUserAgent('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36');
    
//     // Navigate to NBA Visuals
//     await page.goto('https://nbavisuals.com/shotmap', { waitUntil: 'domcontentloaded' });
    
//     // Wait for critical elements
//     await Promise.race([
//       page.waitForSelector('#season-dropdown', { timeout: 5000 }),
//       page.waitForSelector('#playerSearch', { timeout: 5000 })
//     ]);
    
//     // Select season
//     const seasonFormat = `${year}-${(parseInt(year) + 1).toString().slice(-2)}`;
//     await page.select('#season-dropdown', seasonFormat);
    
//     // Select player
//     await page.evaluate(() => {
//       const searchInput = document.getElementById('playerSearch');
//       if (searchInput) searchInput.value = '';
//     });
    
//     await page.type('#playerSearch', playerName);
//     await page.waitForTimeout(1000); // Wait for dropdown
    
//     // Try to select player via dropdown click
//     const playerSelected = await page.evaluate((targetPlayer) => {
//       const dropdown = document.querySelector('.choices__list--dropdown');
//       if (!dropdown) return false;
      
//       const options = dropdown.querySelectorAll('.choices__item--choice');
      
//       for (const option of options) {
//         const text = option.textContent || '';
//         const cleanText = text.trim();
        
//         if (cleanText.toLowerCase().includes(targetPlayer.toLowerCase())) {
//           option.click();
//           return true;
//         }
//       }
      
//       return false;
//     }, playerName);
    
//     if (!playerSelected) {
//       // Fallback: use keyboard
//       await page.keyboard.press('ArrowDown');
//       await page.waitForTimeout(200);
//       await page.keyboard.press('Enter');
//     }
    
//     // Generate shotmap
//     await page.evaluate(() => {
//       const generateButton = document.getElementById('generateGraphButton');
//       if (generateButton) generateButton.click();
//     });
    
//     // Wait for graph with shorter timeout
//     let graphLoaded = false;
//     let attempts = 0;
    
//     while (attempts < 5 && !graphLoaded) {
//       attempts++;
//       await page.waitForTimeout(2000);
      
//       graphLoaded = await page.evaluate(() => {
//         const graphContainer = document.getElementById('graph-container');
//         if (!graphContainer) return false;
        
//         const hasVisual = graphContainer.querySelector('svg, canvas, img, [class*="plot"]');
//         const text = graphContainer.textContent || '';
//         const hasContent = text.length > 50 && text.includes('eFG%');
        
//         return hasVisual || hasContent;
//       });
//     }
    
//     if (!graphLoaded) {
//       console.log('⚠️ Graph may not have loaded completely, proceeding anyway...');
//     }
    
//     // Get graph area
//     const graphContainerInfo = await page.evaluate(() => {
//       const graphContainer = document.getElementById('graph-container');
//       if (graphContainer && graphContainer.children.length > 0) {
//         const rect = graphContainer.getBoundingClientRect();
//         return {
//           x: rect.x,
//           y: rect.y,
//           width: rect.width,
//           height: rect.height
//         };
//       }
      
//       // Fallbacks
//       const shotmapVis = document.querySelector('[class*="shotmap"], [class*="visualization"]');
//       if (shotmapVis) {
//         const rect = shotmapVis.getBoundingClientRect();
//         return {
//           x: rect.x,
//           y: rect.y,
//           width: rect.width,
//           height: rect.height
//         };
//       }
      
//       return {
//         x: 50,
//         y: 200,
//         width: 900,
//         height: 700
//       };
//     });
    
//     // Take screenshot
//     const screenshot = await page.screenshot({
//       type: 'png',
//       clip: {
//         x: Math.max(0, graphContainerInfo.x - 10),
//         y: Math.max(0, graphContainerInfo.y - 10),
//         width: Math.min(graphContainerInfo.width + 20, 1200),
//         height: Math.min(graphContainerInfo.height + 20, 800)
//       },
//       encoding: 'binary'
//     });
    
//     console.log('✅ Shotmap generated successfully');
//     return screenshot;
    
//   } catch (error) {
//     console.error('❌ Error generating shotmap:', error.message);
//     throw error;
//   } finally {
//     await page.close();
//   }
// }

// module.exports = { generateShotmap };












// Working but took 11 seconds

// const puppeteer = require('puppeteer');
// const fs = require('fs');
// const path = require('path');

// let browser = null;

// async function getBrowser() {
//   if (!browser) {
//     browser = await puppeteer.launch({
//       headless: 'new',
//       args: ['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage'],
//       defaultViewport: { width: 1400, height: 1000 }
//     });
//   }
//   return browser;
// }

// function getCacheFilename(playerName, year, modern = false) {
//   const cleanName = playerName
//     .toLowerCase()
//     .replace(/[^a-z0-9]/g, '_')
//     .replace(/_+/g, '_')
//     .replace(/^_|_$/g, '');
  
//   const modernSuffix = modern ? '_modern' : '';
//   return `${cleanName}${modernSuffix}_${year}.png`;
// }

// function checkCache(filename) {
//   const cachePath = path.join(__dirname, '..', 'cache', filename);
//   if (fs.existsSync(cachePath)) {
//     console.log(`📂 Loading from cache: ${filename}`);
//     return fs.readFileSync(cachePath);
//   }
//   return null;
// }

// function saveToCache(filename, screenshotBuffer) {
//   const cacheDir = path.join(__dirname, '..', 'cache');
  
//   if (!fs.existsSync(cacheDir)) {
//     fs.mkdirSync(cacheDir, { recursive: true });
//   }
  
//   const cachePath = path.join(cacheDir, filename);
//   fs.writeFileSync(cachePath, screenshotBuffer);
//   console.log(`💾 Saved to cache: ${filename}`);
// }

// async function generateShotmap(playerName, year, seasonType = 'RS', options = {}) {
//   const { modern = false } = options;
//   const cacheFilename = getCacheFilename(playerName, year, modern);
  
//   // Check cache first
//   const cached = checkCache(cacheFilename);
//   if (cached) {
//     return cached;
//   }
  
//   console.log(`🎯 Generating shotmap for: ${playerName} - ${year} ${modern ? '(Modern)' : ''}`);
  
//   const browser = await getBrowser();
//   const page = await browser.newPage();
  
//   try {
//     page.setDefaultTimeout(30000);
//     page.setDefaultNavigationTimeout(30000);
    
//     await page.setUserAgent('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36');
    
//     console.log('🔗 Navigating to nbavisuals.com/shotmap...');
//     await page.goto('https://nbavisuals.com/shotmap', { 
//       waitUntil: 'domcontentloaded',
//       timeout: 20000 
//     });
    
//     console.log('✅ Page loaded');
    
//     // Wait for form elements
//     await page.waitForSelector('#season-dropdown', { timeout: 5000 });
//     await page.waitForSelector('#playerSearch', { timeout: 5000 });
    
//     // 1) Select Season
//     const seasonFormat = `${year}-${(parseInt(year) + 1).toString().slice(-2)}`;
//     console.log(`📅 Selecting season: ${seasonFormat}`);
//     await page.select('#season-dropdown', seasonFormat);
//     await page.waitForTimeout(500);
    
//     // 2) Select Player using mouse first then arrowdown + Enter
//     console.log(`👤 Selecting player: ${playerName}`);
    
//     // Clear any existing selections
//     await page.evaluate(() => {
//       const searchInput = document.getElementById('playerSearch');
//       if (searchInput) {
//         searchInput.value = '';
//         searchInput.focus();
//       }
//     });
    
//     // Type player name
//     await page.type('#playerSearch', playerName, { delay: 100 });
//     await page.waitForTimeout(1500); // Wait for dropdown
    
//     // MOUSE-FIRST approach: Try to click dropdown option
//     console.log('   Using mouse-first selection...');
//     const mouseSelectionSuccess = await page.evaluate((targetPlayer) => {
//       const dropdown = document.querySelector('.choices__list--dropdown');
//       if (!dropdown) {
//         console.log('   No dropdown found');
//         return false;
//       }
      
//       const options = dropdown.querySelectorAll('.choices__item--choice');
//       console.log(`   Found ${options.length} dropdown options`);
      
//       // Try exact or partial match
//       for (const option of options) {
//         const text = option.textContent || '';
//         const cleanText = text.trim();
        
//         if (cleanText.toLowerCase().includes(targetPlayer.toLowerCase())) {
//           console.log(`   Found match, clicking: "${cleanText}"`);
//           option.click();
//           return true;
//         }
//       }
      
//       return false;
//     }, playerName);
    
//     if (!mouseSelectionSuccess) {
//       console.log('   Mouse selection failed, using keyboard fallback...');
//       // KEYBOARD FALLBACK: ArrowDown + Enter
//       await page.keyboard.press('ArrowDown');
//       await page.waitForTimeout(300);
//       await page.keyboard.press('Enter');
//     }
    
//     await page.waitForTimeout(1000);
    
//     // 3) Toggle Modern Shotmap (AFTER player selection)
//     if (modern) {
//       console.log('🔄 Toggling Modern Shotmap...');
      
//       const modernToggled = await page.evaluate(() => {
//         // Look for the Modern Shotmap checkbox
//         // It's likely a checkbox input with a label containing "Modern Shotmap"
//         const allElements = document.querySelectorAll('*');
        
//         for (const el of allElements) {
//           const text = el.textContent || '';
          
//           // Look for checkbox or switch
//           if (el.tagName === 'INPUT' && el.type === 'checkbox') {
//             // Check if associated label contains "Modern Shotmap"
//             const labels = document.querySelectorAll('label');
//             for (const label of labels) {
//               if ((label.htmlFor === el.id || label.contains(el)) && 
//                   label.textContent.includes('Modern Shotmap')) {
//                 console.log('Found Modern Shotmap checkbox via label');
//                 el.click();
//                 return true;
//               }
//             }
//           }
          
//           // Look for element with exact "Modern Shotmap" text
//           if (text.trim() === 'Modern Shotmap') {
//             console.log('Found element with "Modern Shotmap" text');
            
//             // Try to find checkbox near this element
//             const checkbox = el.querySelector('input[type="checkbox"]');
//             if (checkbox) {
//               checkbox.click();
//               return true;
//             }
            
//             // Click the element itself (might be a label or span)
//             el.click();
//             return true;
//           }
//         }
        
//         console.log('Modern Shotmap element not found');
//         return false;
//       });
      
//       if (modernToggled) {
//         console.log('✅ Modern Shotmap toggled');
//         await page.waitForTimeout(500); // Brief wait for UI update
//       } else {
//         console.log('⚠️ Could not toggle Modern Shotmap');
//       }
//     }
    
//     // 4) Click Generate Button
//     console.log('🔄 Clicking Generate Shotmap button...');
    
//     const generateClicked = await page.evaluate(() => {
//       const generateButton = document.getElementById('generateGraphButton');
//       if (generateButton) {
//         // Check if button is enabled
//         if (!generateButton.disabled) {
//           generateButton.click();
//           console.log('Generate button clicked');
//           return true;
//         } else {
//           console.log('Generate button is disabled');
//           return false;
//         }
//       }
//       console.log('Generate button not found');
//       return false;
//     });
    
//     if (!generateClicked) {
//       throw new Error('Could not click Generate button (might be disabled)');
//     }
    
//     // 5) Wait for graph and Screenshot
//     console.log('⏳ Waiting for shotmap to generate...');
    
//     let graphLoaded = false;
//     let attempts = 0;
//     const maxAttempts = 10; // 20 seconds total (10 * 2000ms)
    
//     while (attempts < maxAttempts && !graphLoaded) {
//       attempts++;
//       await page.waitForTimeout(2000);
      
//       graphLoaded = await page.evaluate(() => {
//         const graphContainer = document.getElementById('graph-container');
//         if (!graphContainer) return false;
        
//         // Check for visual elements
//         const hasVisual = graphContainer.querySelector('svg, canvas, img');
        
//         // Check for shotmap content
//         const text = graphContainer.textContent || '';
//         const hasShotmapContent = text.includes('eFG%') || text.includes('Shots:') || text.includes('Zone');
        
//         return hasVisual || hasShotmapContent;
//       });
      
//       if (!graphLoaded) {
//         console.log(`   Attempt ${attempts}/${maxAttempts}: Waiting...`);
//       }
//     }
    
//     if (!graphLoaded) {
//       console.log('⚠️ Graph may not have loaded completely, capturing anyway');
//     } else {
//       console.log('✅ Graph loaded');
//     }
    
//     // Final wait for rendering
//     await page.waitForTimeout(1000);
    
//     // Get graph area for screenshot
//     const graphContainerInfo = await page.evaluate(() => {
//       const graphContainer = document.getElementById('graph-container');
//       if (graphContainer && graphContainer.children.length > 0) {
//         const rect = graphContainer.getBoundingClientRect();
//         return {
//           x: rect.x,
//           y: rect.y,
//           width: rect.width,
//           height: rect.height
//         };
//       }
      
//       // Fallback
//       return {
//         x: 50,
//         y: 200,
//         width: 900,
//         height: 700
//       };
//     });
    
//     // Take screenshot
//     console.log('📸 Taking screenshot...');
//     const screenshot = await page.screenshot({
//       type: 'png',
//       clip: {
//         x: Math.max(0, graphContainerInfo.x - 10),
//         y: Math.max(0, graphContainerInfo.y - 10),
//         width: Math.min(graphContainerInfo.width + 20, 1200),
//         height: Math.min(graphContainerInfo.height + 20, 800)
//       },
//       encoding: 'binary'
//     });
    
//     // Save to cache
//     saveToCache(cacheFilename, screenshot);
    
//     console.log('✅ Shotmap generated successfully');
//     return screenshot;
    
//   } catch (error) {
//     console.error('❌ Error generating shotmap:', error.message);
//     throw error;
//   } finally {
//     await page.close();
//   }
// }

// // Command parsing function
// async function handleShotmapCommand(command) {
//   const parts = command.split(' ');
  
//   if (parts[0] !== '!shotmap' && parts[0] !== '!shotchart') {
//     throw new Error('Invalid command format');
//   }
  
//   let playerNameParts = [];
//   let year = null;
//   let modern = false;
  
//   for (let i = 1; i < parts.length; i++) {
//     const part = parts[i];
    
//     if (part === '--modern') {
//       modern = true;
//     } else if (/^\d{4}$/.test(part)) {
//       year = part;
//     } else if (part !== '!shotmap' && part !== '!shotchart') {
//       playerNameParts.push(part);
//     }
//   }
  
//   if (!year) {
//     throw new Error('Year not specified');
//   }
  
//   const playerName = playerNameParts.join(' ');
  
//   if (!playerName) {
//     throw new Error('Player name not specified');
//   }
  
//   return generateShotmap(playerName, year, 'RS', { modern });
// }

// module.exports = { 
//   generateShotmap,
//   handleShotmapCommand
// };
























const puppeteer = require('puppeteer');
const fs = require('fs');
const path = require('path');

let browser = null;

async function getBrowser() {
  if (!browser) {
    console.log('🌐 Launching browser...');
    browser = await puppeteer.launch({
      headless: 'new',
      args: ['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage'],
      defaultViewport: { width: 1400, height: 1000 }
    });
  }
  return browser;
}

function getCacheFilename(playerName, year, modern = false) {
  const cleanName = playerName
    .toLowerCase()
    .replace(/[^a-z0-9]/g, '_')
    .replace(/_+/g, '_')
    .replace(/^_|_$/g, '');
  
  const modernSuffix = modern ? '_modern' : '';
  return `${cleanName}${modernSuffix}_${year}.png`;
}

function checkCache(filename) {
  const cachePath = path.join(__dirname, '..', 'cache', filename);
  if (fs.existsSync(cachePath)) {
    console.log(`📂 Loading from cache: ${filename}`);
    return fs.readFileSync(cachePath);
  }
  return null;
}

function saveToCache(filename, screenshotBuffer) {
  const cacheDir = path.join(__dirname, '..', 'cache');
  
  if (!fs.existsSync(cacheDir)) {
    fs.mkdirSync(cacheDir, { recursive: true });
  }
  
  const cachePath = path.join(cacheDir, filename);
  fs.writeFileSync(cachePath, screenshotBuffer);
  console.log(`💾 Saved to cache: ${filename}`);
}

async function generateShotmap(playerName, year, seasonType = 'RS', options = {}) {
  const { modern = false } = options;
  const cacheFilename = getCacheFilename(playerName, year, modern);
  
  // Check cache first
  const cached = checkCache(cacheFilename);
  if (cached) {
    return cached;
  }
  
  console.log(`🎯 Generating shotmap for: ${playerName} - ${year} ${modern ? '(Modern)' : ''}`);
  
  const browser = await getBrowser();
  const page = await browser.newPage();
  
  try {
    page.setDefaultTimeout(30000);
    page.setDefaultNavigationTimeout(30000);
    
    await page.setUserAgent('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36');
    
    console.log('🔗 Navigating to nbavisuals.com/shotmap...');
    await page.goto('https://nbavisuals.com/shotmap', { 
      waitUntil: 'domcontentloaded',
      timeout: 20000 
    });
    
    console.log('✅ Page loaded');
    
    // Wait for form elements
    await page.waitForSelector('#season-dropdown', { timeout: 5000 });
    
    // 1) Select Season
    const seasonFormat = `${year}-${(parseInt(year) + 1).toString().slice(-2)}`;
    console.log(`📅 Selecting season: ${seasonFormat}`);
    await page.select('#season-dropdown', seasonFormat);
    await page.waitForTimeout(300);
    
    // 2) Select Player
    console.log(`👤 Selecting player: ${playerName}`);
    
    // Clear search input
    await page.evaluate(() => {
      const searchInput = document.getElementById('playerSearch');
      if (searchInput) {
        searchInput.value = '';
        searchInput.focus();
      }
    });
    
    await page.type('#playerSearch', playerName, { delay: 50 });
    await page.waitForTimeout(800); // Wait for dropdown
    
    // Try mouse selection first
    const playerSelected = await page.evaluate((targetPlayer) => {
      const options = document.querySelectorAll('.choices__item--choice');
      for (const option of options) {
        if (option.textContent.toLowerCase().includes(targetPlayer.toLowerCase())) {
          option.click();
          return true;
        }
      }
      return false;
    }, playerName);
    
    if (!playerSelected) {
      console.log('   Using keyboard fallback...');
      await page.keyboard.press('ArrowDown');
      await page.waitForTimeout(100);
      await page.keyboard.press('Enter');
    }
    
    await page.waitForTimeout(300);
    
    // 3) Toggle Modern Shotmap
    if (modern) {
      console.log('🔄 Toggling Modern Shotmap...');
      
      const modernToggled = await page.evaluate(() => {
        const elements = document.querySelectorAll('*');
        for (const el of elements) {
          if (el.textContent?.trim() === 'Modern Shotmap') {
            const checkbox = el.querySelector('input[type="checkbox"]');
            if (checkbox) {
              checkbox.click();
              return true;
            } else {
              el.click();
              return true;
            }
          }
        }
        return false;
      });
      
      if (modernToggled) {
        console.log('✅ Modern Shotmap toggled');
        await page.waitForTimeout(300);
      } else {
        console.log('⚠️ Could not find Modern Shotmap toggle');
      }
    }
    
    // 4) Click Generate Button
    console.log('🔄 Clicking Generate Shotmap button...');
    await page.evaluate(() => {
      document.getElementById('generateGraphButton')?.click();
    });
    
    // 5) Wait for graph
    console.log('⏳ Waiting for shotmap to generate...');
    
    // Initial wait
    await page.waitForTimeout(2000);
    
    // Check for graph with minimal attempts
    for (let i = 0; i < 6; i++) {
      await page.waitForTimeout(1000);
      
      const loaded = await page.evaluate(() => {
        const container = document.getElementById('graph-container');
        if (!container) return false;
        
        // Check for visual elements or shotmap content
        const hasVisual = container.querySelector('svg, canvas, img');
        const hasContent = container.textContent.includes('eFG%') || 
                          container.textContent.includes('Shots:');
        
        return hasVisual || hasContent;
      });
      
      if (loaded) {
        console.log('✅ Graph loaded');
        break;
      }
      
      if (i === 5) console.log('⚠️ Graph may not have loaded completely');
    }
    
    // Final wait for rendering
    await page.waitForTimeout(1000);
    
    // Get graph area for screenshot
    console.log('🔍 Finding graph area...');
    const graphContainerInfo = await page.evaluate(() => {
      const graphContainer = document.getElementById('graph-container');
      if (graphContainer && graphContainer.children.length > 0) {
        const rect = graphContainer.getBoundingClientRect();
        return {
          x: rect.x,
          y: rect.y,
          width: rect.width,
          height: rect.height
        };
      }
      
      // Fallback
      return {
        x: 50,
        y: 200,
        width: 900,
        height: 700
      };
    });
    
    console.log(`📏 Capture area: ${graphContainerInfo.width}x${graphContainerInfo.height}`);
    
    // 6) Take screenshot (ORIGINAL DIMENSIONS)
    console.log('📸 Taking screenshot...');
    const screenshot = await page.screenshot({
      type: 'png',
      clip: {
        x: Math.max(0, graphContainerInfo.x - 10),
        y: Math.max(0, graphContainerInfo.y - 10),
        width: Math.min(graphContainerInfo.width + 20, 1200),
        height: Math.min(graphContainerInfo.height + 20, 800)
      },
      encoding: 'binary'
    });
    
    // Save to cache
    saveToCache(cacheFilename, screenshot);
    
    console.log('✅ Shotmap generated successfully');
    return screenshot;
    
  } catch (error) {
    console.error('❌ Error generating shotmap:', error.message);
    throw error;
  } finally {
    await page.close();
  }
}

// Command parsing function
async function handleShotmapCommand(command) {
  const parts = command.split(' ');
  
  if (parts[0] !== '!shotmap' && parts[0] !== '!shotchart') {
    throw new Error('Invalid command format');
  }
  
  let playerNameParts = [];
  let year = null;
  let modern = false;
  
  for (let i = 1; i < parts.length; i++) {
    const part = parts[i];
    
    if (part === '--modern') {
      modern = true;
    } else if (/^\d{4}$/.test(part)) {
      year = part;
    } else if (part !== '!shotmap' && part !== '!shotchart') {
      playerNameParts.push(part);
    }
  }
  
  if (!year) {
    throw new Error('Year not specified');
  }
  
  const playerName = playerNameParts.join(' ');
  
  if (!playerName) {
    throw new Error('Player name not specified');
  }
  
  return generateShotmap(playerName, year, 'RS', { modern });
}

module.exports = { 
  generateShotmap,
  handleShotmapCommand
};