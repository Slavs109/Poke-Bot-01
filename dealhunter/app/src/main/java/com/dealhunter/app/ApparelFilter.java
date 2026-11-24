package com.dealhunter.app;
import java.util.*;import java.util.regex.*;import android.text.Html;
final class ApparelFilter {
 static boolean has(String s,String pattern){return Pattern.compile(pattern,Pattern.CASE_INSENSITIVE).matcher(s).find();}
 static int discount(String title,String description){String t=title.toLowerCase(Locale.US), d=Html.fromHtml(description,Html.FROM_HTML_MODE_LEGACY).toString().toLowerCase(Locale.US), all=t+" "+d;
 if(has(t,"\\b(women|women's|women’s|womens|kids|children|girls|boys|toddler|expired|sold out)\\b"))return 0;
 if(!has(t,"\\b(shoes?|boots?|sneakers?|shirts?|t-shirts?|tees?|jeans|pants|jackets?|hoodies?|shorts|polos?|sweaters?|clothing|apparel|coats?|socks|underwear|chinos|suits?|flannel|outerwear|sandals?|loafers?|workwear|activewear|fleece|joggers|menswear)\\b"))return 0;
 if(!has(all,"\\b(men|mens|men's|men’s|menswear|unisex)\\b"))return 0;
 int best=0;Matcher m=Pattern.compile("(?<!\\d)(\\d{1,2}|100)\\s*%\\s*(?:off|discount|savings)").matcher(t);while(m.find())best=Math.max(best,Integer.parseInt(m.group(1)));
 if(best==0){m=Pattern.compile("(?<!\\d)(\\d{1,2}|100)\\s*%\\s*(?:off|discount|savings)").matcher(d);if(m.find())best=Integer.parseInt(m.group(1));}
 Matcher pair=Pattern.compile("\\$([0-9]+(?:\\.[0-9]{2})?)\\s*(?:\\(|,|;|[-–])?\\s*(?:was|reg(?:ular)?\\.?|originally|list price|msrp)\\s*[: ]*\\$([0-9]+(?:\\.[0-9]{2})?)").matcher(all);
 if(pair.find()){double sale=Double.parseDouble(pair.group(1)),regular=Double.parseDouble(pair.group(2));if(regular>sale&&sale>=0)best=Math.max(best,(int)Math.floor(100*(regular-sale)/regular));}
 Matcher stack=Pattern.compile("(?<!\\d)(\\d{1,2})\\s*%\\s*off\\s*\\+\\s*extra\\s*(\\d{1,2})\\s*%\\s*off").matcher(t);
 if(stack.find()){int first=Integer.parseInt(stack.group(1)),extra=Integer.parseInt(stack.group(2));best=Math.max(best,(int)Math.floor(100-(100-first)*(100-extra)/100.0));}
 return best;
 }
}
