// Working but took 11 seconds - Seems to be the one we are using now.

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
//   const cacheDir = '/data/cache';
  
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
//       function normalize(str) {
//         return str
//           .toLowerCase()
//           .replace(/[’']/g, '')       // remove apostrophes
//           .replace(/[^a-z0-9 ]/g, '') // remove special chars
//           .replace(/\s+/g, ' ')       // normalize spaces
//           .trim();
//       }

//       const dropdown = document.querySelector('.choices__list--dropdown');
//       if (!dropdown) return false;

//       const options = dropdown.querySelectorAll('.choices__item--choice');

//       const normalizedTarget = normalize(targetPlayer);

//       for (const option of options) {
//         const text = option.textContent || '';
//         const normalizedOption = normalize(text);

//         if (normalizedOption.includes(normalizedTarget)) {
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



// 7.25 seconds below.

const puppeteer = require('puppeteer');
const fs = require('fs');
const path = require('path');

let browser = null;

async function getBrowser() {
  if (!browser) {
    browser = await puppeteer.launch({
      headless: 'new',
      args: [
        '--no-sandbox',
        '--disable-setuid-sandbox',
        '--disable-dev-shm-usage',
      ],
      defaultViewport: { width: 1400, height: 1000 },
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
  const cachePath = path.join('/data/cache', filename);
  if (fs.existsSync(cachePath)) {
    console.log(`📂 Loading from cache: ${filename}`);
    return fs.readFileSync(cachePath);
  }
  return null;
}

function saveToCache(filename, screenshotBuffer) {
  const cacheDir = '/data/cache';
  if (!fs.existsSync(cacheDir)) fs.mkdirSync(cacheDir, { recursive: true });
  const cachePath = path.join(cacheDir, filename);
  fs.writeFileSync(cachePath, screenshotBuffer);
  console.log(`💾 Saved to cache: ${filename}`);
}

async function generateShotmap(playerName, year, seasonType = 'RS', options = {}) {
  const { modern = false } = options;
  const cacheFilename = getCacheFilename(playerName, year, modern);
  const cached = checkCache(cacheFilename);
  if (cached) return cached;

  console.log(`🎯 Generating shotmap for: ${playerName} - ${year} ${modern ? '(Modern)' : ''}`);

  const browser = await getBrowser();
  const page = await browser.newPage();

  try {
    page.setDefaultTimeout(20000);
    page.setDefaultNavigationTimeout(20000);

    await page.setUserAgent(
      'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    );

    console.log('🔗 Navigating to nbavisuals.com/shotmap...');
    await page.goto('https://nbavisuals.com/shotmap', {
      waitUntil: 'domcontentloaded',
      timeout: 10000,
    });

    await page.waitForSelector('#season-dropdown');
    await page.waitForSelector('#playerSearch');
    console.log('✅ Page loaded');

    const seasonFormat = `${year}-${(parseInt(year) + 1)
      .toString()
      .slice(-2)}`;
    console.log(`📅 Selecting season: ${seasonFormat}`);
    await page.select('#season-dropdown', seasonFormat);

    console.log(`👤 Selecting player: ${playerName}`);
    await page.evaluate(() => {
      const el = document.getElementById('playerSearch');
      if (el) {
        el.value = '';
        el.focus();
      }
    });

    await page.type('#playerSearch', playerName, { delay: 50 });

    // 🔎 Wait for dropdown to be populated OR timeout quickly
    console.log('   Waiting for player dropdown...');
    await Promise.race([
      page.waitForSelector('.choices__list--dropdown .choices__item--choice', {
        visible: true,
      }),
      page.waitForTimeout(1200),
    ]);

    const selected = await page.evaluate((targetPlayer) => {
      function normalize(str) {
        return str
          .toLowerCase()
          .replace(/[’']/g, '')
          .replace(/[^a-z0-9 ]/g, '')
          .replace(/\s+/g, ' ')
          .trim();
      }
      const dropdown = document.querySelector('.choices__list--dropdown');
      if (!dropdown) return false;
      const options = dropdown.querySelectorAll('.choices__item--choice');
      const normalizedTarget = normalize(targetPlayer);
      for (const option of options) {
        const text = option.textContent || '';
        const normalized = normalize(text);
        if (normalized.includes(normalizedTarget)) {
          option.dispatchEvent(new MouseEvent('click', { bubbles: true }));
          return true;
        }
      }
      return false;
    }, playerName);

    if (!selected) {
      await page.keyboard.press('ArrowDown');
      await page.keyboard.press('Enter');
    }

    // ⚡ SHORT waits (allow quick UI update)
    await page.waitForTimeout(500);

    if (modern) {
      console.log('🔄 Toggling Modern Shotmap...');
      await page.evaluate(() => {
        const labels = document.querySelectorAll('label');
        for (const label of labels) {
          if (label.textContent.includes('Modern Shotmap')) {
            const input = label.querySelector('input[type="checkbox"]');
            if (input) input.click();
            break;
          }
        }
      });
      await page.waitForTimeout(300);
    }

    console.log('🔄 Clicking Generate Shotmap...');
    const clicked = await page.evaluate(() => {
      const btn = document.getElementById('generateGraphButton');
      if (btn && !btn.disabled) {
        btn.click();
        return true;
      }
      return false;
    });
    if (!clicked) throw new Error('Generate button missing or disabled');

    console.log('⏳ Waiting for graph...');
    // replace polling loop with dynamic wait
    await page.waitForFunction(
      () => {
        const gc = document.getElementById('graph-container');
        if (!gc) return false;
        return gc.querySelector('svg,canvas,img');
      },
      { timeout: 10000 }
    );

    // short render delay
    await page.waitForTimeout(400);

    const graphRect = await page.evaluate(() => {
      const gc = document.getElementById('graph-container');
      const rect = gc?.getBoundingClientRect?.();
      return rect
        ? { x: rect.x, y: rect.y, width: rect.width, height: rect.height }
        : { x: 50, y: 200, width: 900, height: 700 };
    });

    console.log('📸 Taking screenshot...');
    const screenshot = await page.screenshot({
      type: 'png',
      clip: {
        x: Math.max(0, graphRect.x - 10),
        y: Math.max(0, graphRect.y - 10),
        width: Math.min(graphRect.width + 20, 1200),
        height: Math.min(graphRect.height + 20, 800),
      },
      encoding: 'binary',
    });

    saveToCache(cacheFilename, screenshot);
    console.log('✅ Shotmap generated successfully');
    return screenshot;
  } catch (err) {
    console.error('❌ Error generating shotmap:', err.message);
    throw err;
  } finally {
    await page.close();
  }
}

async function handleShotmapCommand(command) {
  const parts = command.split(' ');
  if (parts[0] !== '!shotmap' && parts[0] !== '!shotchart')
    throw new Error('Invalid command format');

  let playerNameParts = [];
  let year = null;
  let modern = false;

  for (let i = 1; i < parts.length; i++) {
    const part = parts[i];
    if (part === '--modern') modern = true;
    else if (/^\d{4}$/.test(part)) year = part;
    else playerNameParts.push(part);
  }

  if (!year) throw new Error('Year not specified');
  const playerName = playerNameParts.join(' ');
  if (!playerName) throw new Error('Player name not specified');

  return generateShotmap(playerName, year, 'RS', { modern });
}

module.exports = { generateShotmap, handleShotmapCommand };

































// Works but takes a screenshot of the whole page and not the shot map only, has potental to grow.

// const puppeteer = require('puppeteer');
// const fs = require('fs');
// const path = require('path');

// let browser = null;

// async function getBrowser() {
//   if (!browser) {
//     console.log('🌐 Launching browser...');
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
//   const cleanPlayerName = playerName.trim();

//   const cacheFilename = getCacheFilename(cleanPlayerName, year, modern);
//   const cached = checkCache(cacheFilename);
//   if (cached) return cached;

//   console.log(`🎯 Generating shotmap for: "${cleanPlayerName}" - ${year}`);

//   const browser = await getBrowser();
//   const page = await browser.newPage();

//   try {
//     await page.setViewport({ width: 1400, height: 900 });
//     page.setDefaultTimeout(60000); // Increased timeout

//     // Set realistic user agent
//     await page.setUserAgent(
//       'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
//     );

//     // Add extra headers to look like a real browser
//     await page.setExtraHTTPHeaders({
//       'Accept-Language': 'en-US,en;q=0.9',
//       'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
//       'Accept-Encoding': 'gzip, deflate, br',
//     });

//     console.log('🔗 Navigating to nbavisuals...');
//     await page.goto('https://nbavisuals.com/shotmap', {
//       waitUntil: 'networkidle2', // Changed to networkidle2 for better compatibility
//       timeout: 45000
//     });

//     // Wait for the page to fully load
//     await page.waitForSelector('#season-dropdown', { visible: true });
//     await page.waitForSelector('#playerSearch', { visible: true });
//     await page.waitForTimeout(1000);

//     // --- STEP 1: SELECT SEASON/YEAR ---
//     console.log(`📅 Selecting season for year ${year}...`);
//     const seasonValue = `${year}-${(parseInt(year) + 1).toString().slice(-2)}`;
    
//     // Clear any existing selections first
//     await page.evaluate(() => {
//       const select = document.getElementById('season-dropdown');
//       if (select) {
//         // Clear all selected options
//         Array.from(select.options).forEach(option => {
//           option.selected = false;
//         });
//         // Trigger change event
//         select.dispatchEvent(new Event('change', { bubbles: true }));
//       }
//     });
    
//     await page.waitForTimeout(500);
    
//     // Select the season
//     await page.select('#season-dropdown', seasonValue);
//     console.log(`   Selected: ${seasonValue}`);
    
//     // Wait for season data to load (this is critical!)
//     await page.waitForTimeout(3000);

//     // --- STEP 2: CLEAR EXISTING PLAYER ---
//     console.log('🧹 Clearing any existing player selection...');
    
//     await page.evaluate(() => {
//       // First try to clear using the Choices.js library if it exists
//       if (window.Choices) {
//         const playerSearch = document.getElementById('playerSearch');
//         if (playerSearch && playerSearch.choices) {
//           playerSearch.choices.removeActiveItems();
//         }
//       }
      
//       // Clear the input field
//       const input = document.getElementById('playerSearch');
//       if (input) {
//         input.value = '';
//         input.dispatchEvent(new Event('input', { bubbles: true }));
//         input.dispatchEvent(new Event('change', { bubbles: true }));
//       }
      
//       // Also clear any selected items in the dropdown
//       const selectedItems = document.querySelectorAll('.choices__item--selectable');
//       selectedItems.forEach(item => {
//         const removeBtn = item.querySelector('.choices__button');
//         if (removeBtn) removeBtn.click();
//       });
//     });
    
//     await page.waitForTimeout(1000);

//     // --- STEP 3: TYPE PLAYER NAME ---
//     console.log(`👤 Typing player name: "${cleanPlayerName}"`);
    
//     // Focus on the search input
//     await page.focus('#playerSearch');
//     await page.waitForTimeout(300);
    
//     // Type the player name character by character (slower for dropdown to respond)
//     await page.type('#playerSearch', cleanPlayerName, { delay: 80 });
    
//     // Wait for dropdown to populate (this is key!)
//     await page.waitForTimeout(2000);

//     // --- STEP 4: SELECT PLAYER FROM DROPDOWN ---
//     console.log('🔍 Selecting player from dropdown...');
    
//     // First, try the keyboard method (Arrow Down + Enter)
//     try {
//       // Press ArrowDown to select the first option
//       await page.keyboard.press('ArrowDown');
//       await page.waitForTimeout(300);
      
//       // Press Enter to confirm selection
//       await page.keyboard.press('Enter');
//       console.log('   Selected via keyboard navigation');
//     } catch (error) {
//       console.log('   Keyboard selection failed, trying alternative method...');
//     }
    
//     // Wait for selection to apply
//     await page.waitForTimeout(1500);

//     // --- STEP 5: VERIFY PLAYER IS SELECTED ---
//     const isPlayerSelected = await page.evaluate(() => {
//       // Check if there are selected items in the choices dropdown
//       const selectedChips = document.querySelectorAll('.choices__item--choice');
//       const inputValue = document.getElementById('playerSearch').value;
//       return selectedChips.length > 0 || inputValue.trim().length > 0;
//     });
    
//     if (!isPlayerSelected) {
//       console.log('⚠️ Player not selected, trying direct click method...');
      
//       // Try clicking the dropdown option directly
//       const playerSelected = await page.evaluate((playerName) => {
//         // Find the dropdown list
//         const dropdownList = document.querySelector('.choices__list--dropdown');
//         if (!dropdownList || dropdownList.children.length === 0) return false;
        
//         // Try to find an option that matches
//         const options = Array.from(dropdownList.children);
//         const searchName = playerName.toLowerCase();
        
//         for (const option of options) {
//           const optionText = option.textContent.toLowerCase();
//           if (optionText.includes(searchName) || searchName.includes(optionText)) {
//             option.click();
//             return true;
//           }
//         }
        
//         // If no exact match, click the first option
//         if (options.length > 0) {
//           options[0].click();
//           return true;
//         }
        
//         return false;
//       }, cleanPlayerName);
      
//       if (!playerSelected) {
//         throw new Error(`Could not select player "${cleanPlayerName}". Player may not exist for the ${year} season.`);
//       }
      
//       await page.waitForTimeout(1000);
//     }

//     // --- STEP 6: SET MODERN MODE (if requested) ---
//     if (modern) {
//       console.log('🔘 Enabling modern shotmap mode');
//       await page.evaluate(() => {
//         const modernToggle = document.getElementById('modernModeToggle');
//         if (modernToggle && !modernToggle.checked) {
//           modernToggle.click();
//         }
//       });
//       await page.waitForTimeout(500);
//     }

//     // --- STEP 7: CLICK GENERATE BUTTON ---
//     console.log('🔄 Clicking Generate Shotmap button...');
    
//     // Find and click the generate button
//     const generateClicked = await page.evaluate(() => {
//       const buttons = document.querySelectorAll('button');
//       for (const button of buttons) {
//         const btnText = button.textContent.toLowerCase();
//         if (btnText.includes('generate shotmap') || btnText.includes('generate')) {
//           button.click();
//           return true;
//         }
//       }
//       return false;
//     });
    
//     if (!generateClicked) {
//       throw new Error('Could not find Generate Shotmap button');
//     }

//     // --- STEP 8: WAIT FOR GRAPH TO LOAD ---
//     console.log('⏳ Waiting for graph to generate...');
    
//     // Wait for Plotly to load data
//     try {
//       await page.waitForFunction(() => {
//         // Check if Plotly exists
//         if (!window.Plotly) return false;
        
//         // Look for plotly plots
//         const plots = document.querySelectorAll('.js-plotly-plot');
//         if (plots.length === 0) return false;
        
//         // Check if any plot has data
//         for (const plot of plots) {
//           if (plot.data && plot.data.length > 0) {
//             // Check for actual data points
//             const hasData = plot.data.some(trace => {
//               return (trace.x && trace.x.length > 0) ||
//                      (trace.y && trace.y.length > 0) ||
//                      (trace.lat && trace.lat.length > 0);
//             });
//             if (hasData) return true;
//           }
//         }
        
//         return false;
//       }, { 
//         timeout: 30000,
//         polling: 1000 
//       });
//     } catch (error) {
//       console.log('⚠️ Plotly check timeout, waiting fixed time instead...');
//     }
    
//     // Wait additional time for rendering
//     await page.waitForTimeout(3000);

//     // --- STEP 9: CAPTURE SCREENSHOT ---
//     console.log('📸 Capturing shotmap...');  
    
//     // Try multiple selectors for the graph
//     let graph = await page.$('.js-plotly-plot');
//     if (!graph) {
//       graph = await page.$('.plot-container');
//     }
//     if (!graph) {
//       graph = await page.$('div[data-type="plotly"]');
//     }
    
//     if (!graph) {
//       console.log('⚠️ Graph container not found, capturing main content area');
//       // Fallback: capture the main content area
//       const mainContent = await page.$('.container.mx-auto.mt-4.max-w-5xl');
//       if (mainContent) {
//         graph = mainContent;
//       }
//     }
    
//     let screenshot;
//     if (graph) {
//       const box = await graph.boundingBox();
//       // Add some padding
//       const padding = 10;
//       screenshot = await page.screenshot({
//         clip: {
//           x: Math.max(0, box.x - padding),
//           y: Math.max(0, box.y - padding),
//           width: box.width + (padding * 2),
//           height: box.height + (padding * 2)
//         }
//       });
//     } else {
//       // Last resort: capture viewport
//       screenshot = await page.screenshot({
//         clip: { x: 100, y: 200, width: 1000, height: 600 }
//       });
//     }

//     saveToCache(cacheFilename, screenshot);
//     console.log('✅ Shotmap generated successfully!');
//     return screenshot;

//   } catch (err) {
//     console.error('❌ Shotmap generation failed:', err.message);
//     console.log('🔄 Trying fallback method...');
    
//     // Try simpler method
//     return await generateShotmapSimple(playerName, year, modern);
//   } finally {
//     await page.close();
//   }
// }

// // Alternative simple method
// async function generateShotmapSimple(playerName, year, modern = false) {
//   console.log(`🔧 Using simple method for ${playerName} ${year}`);
  
//   const browser = await getBrowser();
//   const page = await browser.newPage();
  
//   try {
//     await page.setViewport({ width: 1200, height: 800 });
    
//     // Go to site
//     await page.goto('https://nbavisuals.com/shotmap', {
//       waitUntil: 'networkidle0',
//       timeout: 30000
//     });
    
//     // Select year
//     await page.select('#season-dropdown', `${year}-${(parseInt(year) + 1).toString().slice(-2)}`);
//     await page.waitForTimeout(3000);
    
//     // Clear player
//     await page.focus('#playerSearch');
//     await page.keyboard.down('Control');
//     await page.keyboard.press('A');
//     await page.keyboard.up('Control');
//     await page.keyboard.press('Backspace');
//     await page.waitForTimeout(1000);
    
//     // Type player name
//     await page.type('#playerSearch', playerName, { delay: 100 });
//     await page.waitForTimeout(2000);
    
//     // Select player
//     await page.keyboard.press('ArrowDown');
//     await page.waitForTimeout(200);
//     await page.keyboard.press('Enter');
//     await page.waitForTimeout(1000);
    
//     // Modern mode
//     if (modern) {
//       await page.evaluate(() => {
//         const toggle = document.getElementById('modernModeToggle');
//         if (toggle) toggle.click();
//       });
//       await page.waitForTimeout(500);
//     }
    
//     // Generate
//     await page.evaluate(() => {
//       const btn = document.getElementById('generateGraphButton');
//       if (btn) btn.click();
//     });
    
//     // Wait
//     await page.waitForTimeout(8000);
    
//     // Screenshot
//     const screenshot = await page.screenshot({
//       clip: { x: 100, y: 250, width: 1000, height: 550 }
//     });
    
//     return screenshot;
//   } finally {
//     await page.close();
//   }
// }

// // Command handler
// async function handleShotmapCommand(command) {
//   const parts = command.trim().split(/\s+/);
  
//   if (!['!shotmap', '!shotchart'].includes(parts[0])) {
//     throw new Error('Invalid command');
//   }
  
//   let playerNameParts = [];
//   let year = null;
//   let modern = false;
  
//   // Parse command
//   for (let i = 1; i < parts.length; i++) {
//     const part = parts[i];
    
//     if (part === '--modern' || part === '-modern' || part === 'modern') {
//       modern = true;
//     } else if (/^\d{4}$/.test(part)) {
//       year = part;
//     } else {
//       playerNameParts.push(part);
//     }
//   }
  
//   // Validate
//   if (!year) {
//     throw new Error('Please specify a year (e.g., 2003)');
//   }
  
//   if (playerNameParts.length === 0) {
//     throw new Error('Please specify a player name');
//   }
  
//   const playerName = playerNameParts.join(' ');
  
//   console.log(`📊 Processing: ${playerName} | ${year} | Modern: ${modern}`);
  
//   try {
//     const screenshot = await generateShotmap(playerName, year, 'RS', { modern });
//     return screenshot;
//   } catch (error) {
//     console.error('Error:', error.message);
//     throw new Error(`Failed to generate shotmap for ${playerName} (${year}): ${error.message}`);
//   }
// }

// module.exports = {
//   generateShotmap,
//   handleShotmapCommand
// };