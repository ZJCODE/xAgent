"""
Memory configuration constants and keyword triggers for local memory storage.
"""

# 最大扫描长度
MAX_SCAN_LENGTH = 1000

# 多层级关键字配置，按重要性和常见性分级，避免不必要的大量监测
TRIGGER_KEYWORDS = [
    # 第一层：高频且重要的记忆触发词 - 对话中经常出现且需要优先记忆
    [
        # 记忆和记录指令 - 用户明确要求存储，最高优先级
        r'记住(?:这|那|一下)?', r'请记住', r'帮我记住', r'别忘了',
        r'记一下', r'记下来', r'记录一下', r'保存一下', r'存下来',
        r'备忘一下', r'写下来', r'加入备忘', r'添加到备忘', r'做个标记',

        r'\bremember\s+this\b', r'\bremember\s+that\b', r'\bplease\s+remember\b', r'\bdon\'?t\s+forget\b',
        r'\bnote\s+this(?:\s+down)?\b', r'\bmake\s+a\s+note\b', r'\bsave\s+this\b', 
        r'\bkeep\s+this\b', r'\blog\s+this\b', r'\bstore\s+this\b', r'\bwrite\s+this\s+down\b',
        
        # 偏好表达 - 对话中高频出现的个人偏好
        r'我.{0,4}喜欢', r'我.{0,4}不喜欢', r'我.{0,4}讨厌',
        r'我.{0,4}爱(?!上)', r'我.{0,4}偏好', r'我.{0,4}更喜欢',
        r'\bi\s+(?:\w+\s+){0,3}like\b', r'\bi\s+(?:\w+\s+){0,3}love\b', r'\bi\s+(?:\w+\s+){0,3}hate\b', r'\bi\s+(?:\w+\s+){0,3}enjoy\b',
        r'\bi\s+don\'?t\s+like\b', r'\bi\s+dislike\b', r'\bi\'?m\s+not\s+fond\s+of\b',
        
        # 习惯和特征 - 高频且稳定的个人特征
        r'我.{0,4}习惯', r'我.{0,4}总是', r'我.{0,4}从不',
        r'我.{0,4}经常', r'我.{0,4}常常', r'我.{0,4}通常',
        r'\bi\s+(?:\w+\s+){0,3}usually\b', r'\bi\s+(?:\w+\s+){0,3}always\b', r'\bi\s+(?:\w+\s+){0,3}never\b', r'\bi\s+(?:\w+\s+){0,3}often\b',
        r'\bi\s+typically\b', r'\bi\s+rarely\b', r'\bi\s+sometimes\b', r'\bi\s+occasionally\b', r'\bi\s+tend\s+to\b', r'\bi\s+have\s+a\s+habit\s+of\b',
        
        # 需求和目标 - 用户的重要需求
        r'我.{0,4}需要', r'我.{0,4}想要', r'我.{0,4}希望',
        r'我.{0,4}打算', r'我.{0,4}计划', r'我的目标是'
        r'\bi\s+(?:\w+\s+){0,3}need\b', r'\bi\s+(?:\w+\s+){0,3}want\b', r'\bi\s+(?:\w+\s+){0,3}wish\b', r'\bi\s+(?:\w+\s+){0,2}plan\s+to\b',
        r'\bi\s+intend\s+to\b', r'\bi\s+aim\s+to\b', r'\bi\s+hope\s+to\b', r'\bi\s+aspire\s+to\b', r'\bi\'?m\s+trying\s+to\b'
    ],
    
    # 第二层：重要但中等频率的个人信息
    [
        # 关键个人信息 - 基本身份信息
        r'我住在', r'我来自', r'我叫', r'我的名字是',
        r'我的职业是', r'我在.{1,8}工作', r'我在.{1,8}上班',
        r'我是.{1,8}人',
        # 英文地理位置信息
        r'i\s+live\s+in', r'i\s+am\s+from', r'i\'?m\s+from', r'i\s+come\s+from', r'i\s+was\s+born\s+in', 
        r'i\s+grew\s+up\s+in', r'i\'?m\s+originally\s+from', r'i\'?m\s+based\s+in',
        # 英文职业信息
        r'my\s+job\s+is', r'i\s+work\s+(?:at|for|as|in)', r'i\'?m\s+employed\s+(?:at|by)', 
        r'my\s+profession\s+is', r'my\s+career\s+is', r'i\s+do\s+\w+\s+work',
        # 职业身份表达 - "I'm a/an + 职业"
        r'i\'?m\s+a[n]?\s+(?:teacher|engineer|doctor|lawyer|developer|programmer|designer|manager|consultant|analyst|architect|scientist|researcher|nurse|pilot|chef|artist|writer|musician|photographer|therapist|counselor|accountant|banker|trader)\b',
        r'i\'?m\s+a[n]?\s+(?:software|web|data|system|network|security|mobile)\s+(?:engineer|developer|analyst|architect)\b',
        # 英文年龄信息
        r'i\s+am\s+\d{1,3}\s+years\s+old', r'i\'?m\s+\d{1,3}\s+years\s+old', r'i\'?m\s+\d{1,3}(?:\s|$)',
        r'i\s+just\s+turned\s+\d{1,3}', r'i\s+am\s+(?:twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety)', 
        # 英文性别信息
        r'i\s+am\s+(?:male|female)', r'i\'?m\s+(?:male|female)',
        # 英文姓名信息 - 更精确的模式，避免误匹配
        r'my\s+name\s+is\s+[A-Z][a-zA-Z]+\b', r'my\s+name\'?s\s+[A-Z][a-zA-Z]+\b',
        r'call\s+me\s+[A-Z][a-zA-Z]+\b', r'i\'?m\s+called\s+[A-Z][a-zA-Z]+\b',
        # 只匹配明确的姓名自我介绍模式
        r'\bi\s+am\s+[A-Z][a-zA-Z]+(?:\s|[.,!?]|$)', r'\bi\'?m\s+[A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)*(?:\s|[.,!?]|$)',
        
        # 能力和技能 - 用户的专业能力
        r'我.{0,4}擅长', r'我.{0,4}会(?!上)', r'我.{0,4}不会',
        r'我.{0,4}拿手', r'我.{0,4}熟悉', r'我.{0,4}精通',
        r'\bi\s+(?:\w+\s+){0,2}am\s+good\s+at\b', r'\bi\s+(?:\w+\s+){0,3}can\b', r'\bi\s+(?:\w+\s+){0,3}cannot\b',
        r'\bi\'?m\s+skilled\s+(?:in|at)\b', r'\bi\'?m\s+proficient\s+(?:in|at)\b', r'\bi\'?m\s+experienced\s+(?:in|with)\b',
        r'\bi\'?m\s+trained\s+in\b', r'\bi\s+specialize\s+in\b', r'\bi\s+have\s+experience\s+(?:in|with)\b',
        
        # 个人状态和倾向 - 性格特征描述
        r'我是.{1,8}的人', r'我倾向于', r'我更倾向于',
        r'i\s+am\s+(?:a|an)\s+.{1,15}\s+person', r'i\s+prefer',
        
        # 重要约束和禁忌 - 用户的限制
        r'我不能', r'我不允许', r'我拒绝',
        r'我对.{1,6}过敏', r'我不(?:吃|喝|用)', r'我避免'
        r'i\s+cannot', r'i\s+refuse\s+to', r'i\s+will\s+not',
        r'\bi\'?m\s+allergic\s+to\b', r'\bi\s+don\'?t\s+(?:drink|eat)\b', r'\bi\s+avoid\b', 
        r'\bi\s+have\s+(?:diabetes|allergies|asthma)\b', r'\bi\s+suffer\s+from\b'
    ],
    
    # 第三层：低频但重要的信息
    [
        # 重要经历和事件 - 用户提到的重要经历
        r'我曾经', r'我以前', r'对我来说很重要的是',
        r'我(曾|也)?有过.{0,10}(经历|经验)',
        r'i\s+used\s+to', r'i\s+once', r'what\'?s\s+important\s+to\s+me\s+is',
        r'\bi\s+previously\b', r'\bi\s+formerly\b', r'\bi\s+have\s+(?:worked|lived|been)\b',
        
        # 情感状态 - 重要的情感表达
        r'我很(?:开心|高兴|难过|伤心|生气|担心|焦虑|兴奋|失望|沮丧|烦恼)',
        r'让我(?:开心|难过|生气|担心|焦虑|烦恼)的是'
        r'i\s+am\s+(?:very\s+)?(?:happy|sad|angry|worried|anxious|excited|disappointed|frustrated|concerned)',
        r'\bi\s+feel\s+(?:happy|sad|angry|worried|anxious|excited|disappointed|frustrated|concerned)\b'
    ]
]
