async (page) => {
  // Run with browser_run_code_unsafe(filename=...), local server on port 8941.
  await page.route('**/abacus.jasoncameron.dev/**', route => route.abort());
  const failures = [];
  const check = (ok, label) => { if (!ok) failures.push(label); };
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  for (const width of [360, 390, 430]) {
    await page.setViewportSize({width, height: 844});
    await page.goto('http://127.0.0.1:8941/.preview/mobile-fixture/.preview/2026-10-02/index.html');
    for (const n of [1, 2, 3]) {
      await page.locator(`[data-tab="${n}"]`).click();
      const dimensions = await page.evaluate(() => ({width: innerWidth, scroll: document.documentElement.scrollWidth}));
      check(dimensions.scroll <= dimensions.width, `${width}: tab${n} page overflow ${dimensions.scroll}`);
    }
    await page.locator('[data-tab="1"]').click();
    const schedule = await page.locator('.sched-row').evaluateAll(rows => rows.map(row => ({
      date: row.querySelector('.sched-date').getBoundingClientRect().x,
      tag: row.querySelector('.sched-tag').getBoundingClientRect().x,
      count: row.querySelector('.sched-count').getBoundingClientRect().x
    })));
    check(schedule.length === 2 && Math.abs(schedule[0].count-schedule[1].count) < 1,
      `${width}: schedule columns do not align`);
    await page.locator('[data-tab="3"]').click();
    const radar = page.locator('[data-code="9005"]');
    check(await radar.getByText('報價未取得').isVisible(), `${width}: missing quote hidden`);
    const details = radar.locator('details');
    await details.locator('summary').click();
    check(await details.getAttribute('open') !== null, `${width}: details failed to open`);
    check(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `${width}: expanded overflow`);
    const priceDetails = page.locator('[data-code="9006"] details');
    await priceDetails.locator('summary').click();
    check(await priceDetails.getByText('第二款', {exact:true}).isVisible(), `${width}: threshold detail missing`);
    check(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `${width}: full price conditions overflow`);
    await page.locator('#stockSearch').fill('9004');
    check(await page.locator('[data-code="9004"]').isVisible(), `${width}: release search missing`);
    check(!(await page.locator('#empty-3').isVisible()), `${width}: false empty despite release match`);
    await page.locator('#stockSearch').fill('不存在的股票');
    check(await page.locator('#empty-3').isVisible(), `${width}: missing filter empty state`);
    check(await page.locator('[data-section="radar"] .source-warning').isVisible(), `${width}: filtering hides source warning`);
    await page.locator('#stockSearch').fill('');
    check(await radar.isVisible(), `${width}: clear search fails`);
    check(!(await page.locator('#empty-3').isVisible()), `${width}: stale empty state`);
    check((await page.locator('[data-count="3"]').innerText()).includes('注意累計'), `${width}: badge loses meaning`);
    await page.locator('.chip[data-tag="pcb"]').click();
    check(await radar.isVisible(), `${width}: industry match hidden`);
    check(!(await page.locator('[data-code="9004"]').isVisible()), `${width}: industry mismatch visible`);
    await page.locator('#stockSearch').fill('9004');
    check(await page.locator('#empty-3').isVisible(), `${width}: search and industry not intersected`);
    await page.locator('.chip[data-tag=""]').click();
    check(await page.locator('[data-code="9004"]').isVisible(), `${width}: reset industry lost search`);
    await page.locator('#stockSearch').fill('');
    await page.locator('[data-tab="1"]').click();
    const batch = page.locator('details.card').filter({has: page.locator('[data-code="9001"]')});
    await batch.locator(':scope > summary').click();
    check(await batch.getAttribute('open') === null, `${width}: batch did not collapse`);
    await page.locator('#stockSearch').fill('9001');
    check(await page.locator('[data-code="9001"]').isVisible(), `${width}: search match left collapsed`);
    await page.locator('#stockSearch').fill('');
    check(await batch.getAttribute('open') === null, `${width}: clear search lost prior collapse`);
  }
  check(errors.length === 0, `JS errors: ${errors.join('; ')}`);
  return {widths: [360, 390, 430], failures, passed: failures.length === 0};
}
