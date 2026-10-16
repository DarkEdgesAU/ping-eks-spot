import java.nio.file.*;
import java.nio.charset.StandardCharsets;
import java.util.*;
import com.pingidentity.crypto.HashedPassword;
import org.sourceid.common.CryptoUtil;
import org.jose4j.jwk.*;
import org.jose4j.keys.EllipticCurves;

// Executed inside the selected PF image, using that product's hash implementation.
// Cleartext credentials and private JWKs never go to stdout or command arguments.
class OAuthSeedCredentials {
    public static void main(String[] args) throws Exception {
        Path dir = Path.of("/credentials");
        for (String line : Files.readAllLines(dir.resolve("hash-plan.tsv"))) {
            String alias = line.trim();
            if (alias.isEmpty()) continue;
            String secret = Files.readString(dir.resolve(alias+".secret")).strip();
            Path hashFile = dir.resolve(alias+".hash");
            if (!Files.exists(hashFile)) {
                Files.writeString(hashFile, HashedPassword.fromClearText(secret).toEncodedText());
            }
            HashedPassword hash = HashedPassword.fromEncodedText(Files.readString(hashFile).strip());
            if (!hash.checkSecret(secret) || hash.checkSecret(secret+"-wrong")) {
                throw new IllegalStateException("Client hash verification failed: "+alias);
            }
            Path reversible = dir.resolve(alias+".reversible");
            // This baseline HS256 client uses PF's legacy reversible-secret format.
            // Keep the value in a runtime Secret; obfuscation is not encryption at rest.
            if (!Files.exists(reversible)) Files.writeString(reversible, CryptoUtil.obfuscate(secret, "AES"));
            if (!secret.equals(CryptoUtil.deobfuscate(Files.readString(reversible), "AES"))) {
                throw new IllegalStateException("Reversible client secret verification failed: "+alias);
            }
        }
        Map<String,List<JsonWebKey>> sets = new LinkedHashMap<>();
        for (String line : Files.readAllLines(dir.resolve("jwk-plan.tsv"))) {
            if (line.isBlank()) continue;
            String[] parts = line.split("\t", -1);
            List<JsonWebKey> keys = sets.computeIfAbsent(parts[0], k -> new ArrayList<>());
            Path privateFile = dir.resolve(parts[0]+".private-jwks.json");
            if (Files.exists(privateFile)) continue;
            PublicJsonWebKey key = parts[1].equals("RSA") ? RsaJwkGenerator.generateJwk(3072)
                : EcJwkGenerator.generateJwk(EllipticCurves.P256);
            key.setKeyId(parts[2]);
            if (!parts[3].isEmpty()) key.setUse(parts[3]);
            keys.add(key);
        }
        for (var entry : sets.entrySet()) {
            Path privateFile = dir.resolve(entry.getKey()+".private-jwks.json");
            JsonWebKeySet set;
            if (Files.exists(privateFile)) set = new JsonWebKeySet(Files.readString(privateFile));
            else {
                set = new JsonWebKeySet(entry.getValue());
                Files.writeString(privateFile, set.toJson(JsonWebKey.OutputControlLevel.INCLUDE_PRIVATE));
            }
            String publicJson = set.toJson(JsonWebKey.OutputControlLevel.PUBLIC_ONLY);
            Files.writeString(dir.resolve(entry.getKey()+".jwks"), Base64.getUrlEncoder().withoutPadding()
                .encodeToString(publicJson.getBytes(StandardCharsets.UTF_8)));
        }
        System.out.println("PF client hashes verified; replacement JWKs prepared.");
    }
}
