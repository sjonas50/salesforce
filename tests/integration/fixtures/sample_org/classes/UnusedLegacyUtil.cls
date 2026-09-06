/**
 * Nothing references this class. The orphan resolver should leave it unresolved.
 */
public class UnusedLegacyUtil {
    public static String pad(String s, Integer n) {
        return s.leftPad(n);
    }
}
