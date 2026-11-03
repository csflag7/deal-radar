// 由后台程序每天生成并覆盖。当前为【示例数据】，打折日期都是虚构的，只用来演示界面。
// 品牌购物链接在 S2 逐个核实（verified=false 表示还没核实）。
window.DEAL_DATA = {
  meta: { generated_at: "2026-11-01T07:00:00-04:00", sample: true },
  categories: [
    { id: "fashion", name: "服装鞋包", color: "#3b6fd4" },
    { id: "beauty", name: "美妆护肤", color: "#c2417a" },
    { id: "tech", name: "电子数码", color: "#1f8a70" },
    { id: "home", name: "家居综合", color: "#b7791f" }
  ],
  brands: [
    { id: "lululemon", name: "Lululemon", category: "fashion", shop: { US: "https://shop.lululemon.com", CA: "https://shop.lululemon.com/en-ca" }, verified: false },
    { id: "aritzia", name: "Aritzia", category: "fashion", shop: { US: "https://www.aritzia.com/us/en", CA: "https://www.aritzia.com/en" }, verified: false },
    { id: "arcteryx", name: "Arc'teryx", category: "fashion", shop: { US: "https://arcteryx.com/us/en", CA: "https://arcteryx.com/ca/en" }, verified: false },
    { id: "nike", name: "Nike", category: "fashion", shop: { US: "https://www.nike.com", CA: "https://www.nike.com/ca" }, verified: false },
    { id: "coach", name: "Coach", category: "fashion", shop: { US: "https://www.coach.com", CA: "https://ca.coach.com" }, verified: false },
    { id: "uniqlo", name: "Uniqlo", category: "fashion", shop: { US: "https://www.uniqlo.com/us/en", CA: "https://www.uniqlo.com/ca/en" }, verified: false },
    { id: "sephora", name: "Sephora", category: "beauty", shop: { US: "https://www.sephora.com", CA: "https://www.sephora.com/ca/en" }, verified: false },
    { id: "ulta", name: "Ulta Beauty", category: "beauty", shop: { US: "https://www.ulta.com" }, verified: false },
    { id: "shoppers", name: "Shoppers Drug Mart", category: "beauty", shop: { CA: "https://shop.shoppersdrugmart.ca" }, verified: false },
    { id: "apple", name: "Apple", category: "tech", shop: { US: "https://www.apple.com/shop", CA: "https://www.apple.com/ca/shop" }, verified: false },
    { id: "bestbuy", name: "Best Buy", category: "tech", shop: { US: "https://www.bestbuy.com", CA: "https://www.bestbuy.ca" }, verified: false },
    { id: "dyson", name: "Dyson", category: "tech", shop: { US: "https://www.dyson.com", CA: "https://www.dyson.ca" }, verified: false },
    { id: "samsung", name: "Samsung", category: "tech", shop: { US: "https://www.samsung.com/us", CA: "https://www.samsung.com/ca" }, verified: false },
    { id: "amazon", name: "Amazon", category: "home", shop: { US: "https://www.amazon.com", CA: "https://www.amazon.ca" }, verified: false },
    { id: "costco", name: "Costco", category: "home", shop: { US: "https://www.costco.com", CA: "https://www.costco.ca" }, verified: false },
    { id: "walmart", name: "Walmart", category: "home", shop: { US: "https://www.walmart.com", CA: "https://www.walmart.ca" }, verified: false },
    { id: "target", name: "Target", category: "home", shop: { US: "https://www.target.com" }, verified: false },
    { id: "canadiantire", name: "Canadian Tire", category: "home", shop: { CA: "https://www.canadiantire.ca" }, verified: false }
  ],
  // status: confirmed 已官宣 / rumored 传闻 / leaked 提前泄露 / ended 已结束
  sales: [
    { id: "s1", brand: "amazon", region: "US", event: "Early Black Friday", start: "2026-11-05", end: "2026-11-26", discount: "示例：部分商品最高 40% off", status: "confirmed", sources: [] },
    { id: "s2", brand: "amazon", region: "CA", event: "Early Black Friday", start: "2026-11-06", end: "2026-11-26", discount: "示例：部分商品最高 40% off", status: "confirmed", sources: [] },
    { id: "s3", brand: "sephora", region: "US", event: "Holiday Savings Event", start: "2026-11-01", end: "2026-11-11", discount: "示例：会员 10–20% off", status: "confirmed", sources: [] },
    { id: "s4", brand: "sephora", region: "CA", event: "Holiday Savings Event", start: "2026-11-02", end: "2026-11-12", discount: "示例：会员 10–20% off", status: "confirmed", sources: [] },
    { id: "s5", brand: "lululemon", region: "US", event: "Black Friday", start: "2026-11-26", end: "2026-11-30", discount: "示例：We Made Too Much 加量", status: "rumored", sources: [] },
    { id: "s6", brand: "aritzia", region: "CA", event: "Black Friday", start: "2026-11-25", end: "2026-12-01", discount: "示例：全场最高 50% off", status: "leaked", sources: [] },
    { id: "s7", brand: "arcteryx", region: "CA", event: "Black Friday", start: "2026-11-27", end: "2026-11-30", discount: "示例：指定款 30% off", status: "rumored", sources: [] },
    { id: "s8", brand: "bestbuy", region: "US", event: "Black Friday Deals", start: "2026-11-20", end: "2026-11-29", discount: "示例：电视、笔记本特价", status: "confirmed", sources: [] },
    { id: "s9", brand: "bestbuy", region: "CA", event: "Black Friday", start: "2026-11-20", end: "2026-11-27", discount: "示例：电视、笔记本特价", status: "confirmed", sources: [] },
    { id: "s10", brand: "apple", region: "US", event: "Shopping Event", start: "2026-11-27", end: "2026-11-30", discount: "示例：送礼品卡", status: "rumored", sources: [] },
    { id: "s11", brand: "dyson", region: "CA", event: "Black Friday", start: "2026-11-13", end: "2026-12-02", discount: "示例：吸尘器最高 $200 off", status: "confirmed", sources: [] },
    { id: "s12", brand: "ulta", region: "US", event: "Black Friday", start: "2026-11-22", end: "2026-11-28", discount: "示例：Doorbusters", status: "leaked", sources: [] },
    { id: "s13", brand: "nike", region: "US", event: "Black Friday", start: "2026-11-23", end: "2026-11-30", discount: "示例：额外 25% off", status: "rumored", sources: [] },
    { id: "s14", brand: "costco", region: "US", event: "Holiday Savings", start: "2026-11-06", end: "2026-11-30", discount: "示例：分批次特价", status: "confirmed", sources: [] },
    { id: "s15", brand: "walmart", region: "US", event: "Black Friday Deals", start: "2026-11-09", end: "2026-11-30", discount: "示例：分三波上线", status: "confirmed", sources: [] },
    { id: "s16", brand: "target", region: "US", event: "Black Friday", start: "2026-11-02", end: "2026-11-28", discount: "示例：每周更新", status: "confirmed", sources: [] },
    { id: "s17", brand: "canadiantire", region: "CA", event: "Black Friday", start: "2026-11-20", end: "2026-11-30", discount: "示例：工具、家居", status: "rumored", sources: [] },
    { id: "s18", brand: "shoppers", region: "CA", event: "Beauty Boxing Day", start: "2026-12-26", end: "2026-12-31", discount: "示例：PC Optimum 积分加倍", status: "rumored", sources: [] },
    { id: "s19", brand: "samsung", region: "US", event: "Discover Samsung", start: "2026-11-16", end: "2026-12-01", discount: "示例：手机、电视特价", status: "confirmed", sources: [] },
    { id: "s20", brand: "coach", region: "US", event: "Cyber Monday", start: "2026-11-30", end: "2026-11-30", discount: "示例：全场 30% off", status: "rumored", sources: [] },
    { id: "s21", brand: "uniqlo", region: "CA", event: "Black Friday", start: null, end: null, discount: "示例：已官宣，日期未公布", status: "confirmed", sources: [] },
    { id: "s22", brand: "aritzia", region: "US", event: "Boxing Day", start: "2026-12-26", end: "2026-12-29", discount: "示例：季末清仓", status: "rumored", sources: [] }
  ]
};
